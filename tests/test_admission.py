"""Admission control: estimation, additive gates, staggering, victim choice.

Admission-control regression tests. The incident these guard against: three
tasks declaring no resources were admitted in one scheduler tick, each then
used ~5 GiB plus GPU, and the host thrashed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskqueue.db import open_db
from taskqueue.estimate import estimate_task_resources, predict_task_resources
from taskqueue.resources import ResourceBudget
from taskqueue.victim import pick_victim, score_victim


def _budget(*, used: float, total: float = 64.0, soft: int = 80, hard: int = 95) -> ResourceBudget:
    system = {
        "cpu_total_cores": 8,
        "ram_total_gib": total,
        "gpu_vram_total_gib": 0.0,
        "gpus": [],
    }
    cfg = {
        "cpu_limit_pct": 100, "gpu_limit_pct": 100,
        "ram_limit_pct": soft, "ram_hard_limit_pct": hard,
    }
    b = ResourceBudget(system, cfg)
    b.ram_reserved = used
    return b


def _mk_task(conn, project: str, task_id: str, *, yaml_source: str = "", grp: str = "") -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, "
        "yaml_source, grp) VALUES (?,?,?,?,?,?,?,?)",
        (project, task_id, "python", "queued", now, now, yaml_source, grp),
    )
    conn.commit()


def _mk_run(conn, project: str, task_id: str, *, ram: float, vram: float = 0.0) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO runs (project, task_id, started_at, status, peak_ram_gib, peak_vram_gib) "
        "VALUES (?,?,?,?,?,?)",
        (project, task_id, now, "completed", ram, vram),
    )
    conn.commit()


# --- F1: the gate must be additive ---------------------------------------

def test_non_additive_regression(tmp_path: Path) -> None:
    """Host at 60% of a soft-80% limit; a task estimated at 30% must block.

    This fails against the pre-fix code — it is the incident in miniature.
    """
    b = _budget(used=38.4, total=64.0, soft=80)  # 60% of 64; soft = 51.2
    ok, reason = b.fits(1, 0.0, 19.2)            # +30% -> 57.6 > 51.2
    assert not ok
    assert "soft limit" in reason


# --- F2: undeclared resources must not be free ---------------------------

def test_undeclared_task_gets_floor_estimate(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "q.db")
    _mk_task(conn, "p", "t1")
    est = estimate_task_resources(conn, "p", "t1", {}, default_ram_gib=1.0)
    assert est["ram_gib"] == 1.0
    assert est["source"] == "default"
    assert est["unknown"] is True


def test_declared_beats_floor(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "q.db")
    _mk_task(conn, "p", "t1")
    est = estimate_task_resources(conn, "p", "t1", {"ram_gib": 8.0}, default_ram_gib=1.0)
    assert est["ram_gib"] == 8.0
    assert est["source"] == "declared"
    assert est["unknown"] is False


def test_declared_below_floor_is_not_unknown(tmp_path: Path) -> None:
    """Floor may raise the estimate; declaration still clears the unknown cap."""
    conn = open_db(tmp_path / "q.db")
    _mk_task(conn, "p", "t1")
    est = estimate_task_resources(conn, "p", "t1", {"ram_gib": 0.5}, default_ram_gib=1.0)
    assert est["ram_gib"] == 1.0
    assert est["source"] == "default"
    assert est["unknown"] is False


def test_history_below_floor_is_not_unknown(tmp_path: Path) -> None:
    """Sub-floor peaks still mean we have a footprint signal."""
    conn = open_db(tmp_path / "q.db")
    _mk_task(conn, "p", "t1")
    _mk_run(conn, "p", "t1", ram=0.02)
    est = estimate_task_resources(conn, "p", "t1", {}, default_ram_gib=1.0)
    assert est["ram_gib"] == 1.0
    assert est["unknown"] is False


def test_undeclared_floor_blocks_third_task(tmp_path: Path) -> None:
    """3 undeclared tasks, headroom for 2 -> the third is refused."""
    conn = open_db(tmp_path / "q.db")
    for i in range(3):
        _mk_task(conn, "p", f"t{i}")
    b = _budget(used=0.0, total=10.0, soft=25)  # soft = 2.5 GiB: room for 2x1.0
    admitted = 0
    for i in range(3):
        est = estimate_task_resources(conn, "p", f"t{i}", {}, default_ram_gib=1.0)
        ok, _ = b.fits(1, 0.0, est["ram_gib"])
        if ok:
            b.reserve(1, 0.0, est["ram_gib"])
            admitted += 1
    assert admitted == 2


# --- F9: history is used, and beats a low declaration --------------------

def test_history_prediction_overrides_low_declaration(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "q.db")
    _mk_task(conn, "p", "t1")
    for _ in range(3):
        _mk_run(conn, "p", "t1", ram=6.0)
    est = estimate_task_resources(conn, "p", "t1", {"ram_gib": 1.0}, default_ram_gib=1.0)
    assert est["ram_gib"] == 6.0
    assert est["source"] == "history"


def test_undeclared_gpu_blocked_by_historical_peak(tmp_path: Path) -> None:
    """No gpu_vram_gib declared, but history shows 20 GiB on an 8 GiB card."""
    conn = open_db(tmp_path / "q.db")
    _mk_task(conn, "p", "t1")
    _mk_run(conn, "p", "t1", ram=2.0, vram=20.0)
    est = estimate_task_resources(conn, "p", "t1", {}, default_ram_gib=1.0)
    assert est["vram_gib"] == 20.0

    system = {
        "cpu_total_cores": 8, "ram_total_gib": 64.0,
        "gpu_vram_total_gib": 8.0,
        "gpus": [{"index": 0, "vram_gib": 8.0, "vram_used_gib": 0.0}],
    }
    b = ResourceBudget(system, {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 80})
    ok, reason = b.fits(1, est["vram_gib"], est["ram_gib"])
    assert not ok
    assert "never run on this machine" in reason


# --- F8: matrix siblings share a yaml_source -----------------------------

def test_sibling_prediction_from_shared_yaml_source(tmp_path: Path) -> None:
    """A matrix: expansion gives N tasks, same YAML, no per-task history.

    job_seed0 has run and peaked at 6 GiB; job_seed1 has never run but must
    inherit that estimate rather than falling back to the floor.
    """
    conn = open_db(tmp_path / "q.db")
    src = str(tmp_path / "job.yaml")
    _mk_task(conn, "p", "job_seed0", yaml_source=src)
    _mk_task(conn, "p", "job_seed1", yaml_source=src)
    _mk_run(conn, "p", "job_seed0", ram=6.0)

    pred = predict_task_resources(conn, "p", "job_seed1")
    assert pred["source"] == "sibling"
    assert pred["ram_gib"] == 6.0

    est = estimate_task_resources(conn, "p", "job_seed1", {}, default_ram_gib=1.0)
    assert est["ram_gib"] == 6.0
    assert est["unknown"] is False  # no longer a blind guess


def test_group_prediction_fallback(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "q.db")
    _mk_task(conn, "p", "a", yaml_source="a.yaml", grp="train")
    _mk_task(conn, "p", "b", yaml_source="b.yaml", grp="train")
    _mk_run(conn, "p", "a", ram=4.0)
    pred = predict_task_resources(conn, "p", "b")
    assert pred["source"] == "group"
    assert pred["ram_gib"] == 4.0


# --- F12: fail closed when measurement is unavailable --------------------

def test_fails_closed_when_ram_unreadable(monkeypatch) -> None:
    import taskqueue.resources as R

    def _boom() -> float:
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(R, "ram_used_gib", _boom)
    b = R.ResourceBudget(
        {"cpu_total_cores": 8, "ram_total_gib": 64.0, "gpu_vram_total_gib": 0.0, "gpus": []},
        {"ram_limit_pct": 80},
    )
    assert b.measurement_failed
    ok, reason = b.fits(1, 0.0, 0.1)
    assert not ok
    assert "measurement unavailable" in reason


# --- F10 / question (b): restartability drives victim choice -------------

class _FakeRun:
    def __init__(self, ram: float, progress: float, elapsed: float = 60.0) -> None:
        self.current_ram_gib = ram
        self.current_vram_gib = 0.0
        self.progress_pct = progress
        self._elapsed = elapsed

    def elapsed_sec(self) -> float:
        return self._elapsed


def test_idempotent_task_chosen_over_nearly_done_one(tmp_path: Path) -> None:
    """The old rule (biggest RSS) would kill the 5 GiB job at 90% progress."""
    conn = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, "
        "priority, idempotent) VALUES ('p','idem','python','running',?,?,50,1)", (now, now),
    )
    conn.execute(
        "INSERT INTO tasks (project, task_id, command, status, enqueued_at, updated_at, "
        "priority, idempotent) VALUES ('p','precious','python','running',?,?,50,0)", (now, now),
    )
    conn.commit()

    candidates = [
        (("p", "idem"), _FakeRun(ram=4.0, progress=10.0)),
        (("p", "precious"), _FakeRun(ram=5.0, progress=90.0, elapsed=3600.0)),
    ]
    victim, scored = pick_victim(conn, candidates, needed_gib=4.0, resource="ram")
    assert victim == ("p", "idem")
    assert len(scored) == 2


def test_relief_still_dominates_for_useless_victim() -> None:
    """A task freeing nothing must not be chosen just because it is idempotent."""
    frees_nothing = score_victim(
        freed_gib=0.0, needed_gib=8.0, progress_pct=0, priority=50,
        elapsed_sec=0, restartability=1.0,
    )
    frees_plenty = score_victim(
        freed_gib=8.0, needed_gib=8.0, progress_pct=0, priority=50,
        elapsed_sec=0, restartability=0.0,
    )
    assert frees_plenty < frees_nothing
