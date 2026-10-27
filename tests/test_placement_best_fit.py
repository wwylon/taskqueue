"""PC-1 / PC-2: best-fit claim gate, aging, remote P6 hold."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskqueue.db import open_db, set_config
from taskqueue.scheduler import Node
from taskqueue.placement import (
    clear_remote_reservation,
    get_remote_reservation,
    refresh_remote_reservation,
    score_assignment,
    select_task_for_node,
    task_wait_aged,
    claiming_worker_is_best_fit,
)
from taskqueue.worker_api import claim_next_remote_task


def _insert_task(
    db,
    project="proj",
    task_id="T1",
    status="queued",
    dispatch="remote",
    resources=None,
    priority=50,
    enqueued_at=None,
):
    now = enqueued_at or datetime.now(UTC).isoformat()
    res = json.dumps(resources or {"cpu_cores": 1, "ram_gib": 4.0, "gpu_vram_gib": 0.0})
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, dispatch, priority, "
        "resources, depends_on, exclusive, max_attempts, enabled, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, 'true', '[]', ?, ?, ?, '[]', 0, 0, 1, ?, ?)",
        (project, task_id, status, dispatch, priority, res, now, now),
    )
    db.commit()


def _insert_worker(db, worker_id="w1", ram_gib=16.0, load=None, gpu_vram_gib=0.0):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, "
        "tags, projects, status, last_seen, load_json) "
        "VALUES (?, 'windows', 'x86_64', ?, 8, ?, '[]', ?, 'online', ?, ?)",
        (
            worker_id,
            ram_gib,
            gpu_vram_gib,
            json.dumps(["proj"]),
            now,
            json.dumps(load or {}),
        ),
    )
    db.commit()
    return db.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()


def _claim(db_path, worker_row):
    return claim_next_remote_task(db_path, worker_row, lambda: {"proj": None})


def test_score_vram_term():
    n = Node(node_id="n", ram_gib=32, ram_used_gib=0, gpu_vram_gib=16, vram_used_gib=0)
    assert score_assignment(None, n, 8.0, 0.0) == pytest.approx(24.0)
    assert score_assignment(None, n, 8.0, 4.0) == pytest.approx(24.0 + 12.0)


def test_best_fit_tie_allows_claim():
    a = Node(node_id="a", ram_gib=16, ram_used_gib=0)
    b = Node(node_id="b", ram_gib=16, ram_used_gib=0)
    ok, better = claiming_worker_is_best_fit(a, ram=4.0, gpu=0.0, peer_nodes=[b])
    assert ok and better is None


def test_best_fit_defers_to_roomier():
    tight = Node(node_id="tight", ram_gib=16, ram_used_gib=8)
    roomy = Node(node_id="roomy", ram_gib=32, ram_used_gib=0)
    ok, better = claiming_worker_is_best_fit(tight, ram=4.0, gpu=0.0, peer_nodes=[roomy])
    assert not ok
    assert better is not None and better.node_id == "roomy"


def test_aging_zero_always_aged():
    assert task_wait_aged(None, age_sec=0) is True


def test_aging_recent_not_aged():
    now = datetime.now(UTC)
    recent = (now - timedelta(seconds=10)).isoformat()
    assert task_wait_aged(recent, age_sec=120, now=now) is False
    old = (now - timedelta(seconds=200)).isoformat()
    assert task_wait_aged(old, age_sec=120, now=now) is True


def test_select_skips_when_peer_better():
    row = {
        "project": "proj",
        "task_id": "T1",
        "dispatch": "remote",
        "exclusive": 0,
        "resources": '{"cpu_cores": 1, "ram_gib": 4.0, "gpu_vram_gib": 0.0}',
        "requires": "{}",
        "depends_on": "[]",
        "max_attempts": 0,
        "force_run": 0,
        "requires_services": "[]",
        "enqueued_at": datetime.now(UTC).isoformat(),
    }
    from taskqueue.scheduler import FilterContext, filter_task

    tight = Node(
        node_id="tight", os="windows", arch="x86_64", ram_gib=16, ram_used_gib=8,
        cpu_cores=8, projects=["proj"],
    )
    roomy = Node(
        node_id="roomy", os="windows", arch="x86_64", ram_gib=32, ram_used_gib=0,
        cpu_cores=8, projects=["proj"],
    )

    def ctx_for(_row):
        ctx = FilterContext(online_workers=1, dep_status=lambda p, t: None)
        return ctx, 1.0, 4.0, 0.0, 0

    assert select_task_for_node([row], tight, ctx_for, peer_nodes=[roomy], placement_mode="binpack") is None
    assert select_task_for_node([row], roomy, ctx_for, peer_nodes=[tight], placement_mode="binpack") is row
    assert select_task_for_node([row], tight, ctx_for, peer_nodes=[roomy], placement_mode="first_fit") is row


def test_claim_prefers_roomier_worker(tmp_path: Path):
    clear_remote_reservation()
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    set_config(db, "placement_mode", "binpack")
    set_config(db, "placement_age_sec", "3600")
    _insert_task(db, resources={"cpu_cores": 1, "ram_gib": 8.0, "gpu_vram_gib": 0.0})
    w_tight = _insert_worker(db, "tight", ram_gib=16.0, load={"ram_used_gib": 8.0})
    w_roomy = _insert_worker(db, "roomy", ram_gib=32.0, load={"ram_used_gib": 0.0})

    r_tight = _claim(db_path, w_tight)
    assert r_tight.get("task") is None

    w_roomy = db.execute("SELECT * FROM workers WHERE worker_id='roomy'").fetchone()
    r_roomy = _claim(db_path, w_roomy)
    assert r_roomy.get("task") is not None
    assert r_roomy["task"]["task_id"] == "T1"


def test_claim_first_fit_allows_tight(tmp_path: Path):
    clear_remote_reservation()
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    set_config(db, "placement_mode", "first_fit")
    _insert_task(db, resources={"cpu_cores": 1, "ram_gib": 4.0, "gpu_vram_gib": 0.0})
    _insert_worker(db, "tight", ram_gib=16.0, load={"ram_used_gib": 0.0})
    _insert_worker(db, "roomy", ram_gib=64.0, load={"ram_used_gib": 0.0})
    w_tight = db.execute("SELECT * FROM workers WHERE worker_id='tight'").fetchone()
    r = _claim(db_path, w_tight)
    assert r.get("task") is not None


def test_aging_allows_tight_worker(tmp_path: Path):
    clear_remote_reservation()
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    set_config(db, "placement_mode", "binpack")
    set_config(db, "placement_age_sec", "60")
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    _insert_task(
        db,
        resources={"cpu_cores": 1, "ram_gib": 4.0, "gpu_vram_gib": 0.0},
        enqueued_at=old,
    )
    w_tight = _insert_worker(db, "tight", ram_gib=16.0, load={"ram_used_gib": 4.0})
    _insert_worker(db, "roomy", ram_gib=64.0, load={"ram_used_gib": 0.0})
    r = _claim(db_path, w_tight)
    assert r.get("task") is not None


def test_remote_p6_blocks_filler_on_roomiest(tmp_path: Path):
    clear_remote_reservation()
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    set_config(db, "placement_mode", "first_fit")  # isolate P6 from best-fit
    set_config(db, "reservation_max_ticks", "60")
    set_config(db, "scheduler_interval_sec", "5")
    # Fat task needs 14 GiB; worker has 16 total but 4 used by filler-sized free=12.
    # Actually: fat needs 14, free on worker is 2 (16-14 load) — nothing fits free.
    # Roomiest worker 16 GiB total with load 10 → free 6 < 14, but total >= 14.
    # Small filler 2 GiB would fit free 6 unless hold reserves 14.
    _insert_task(
        db,
        task_id="FAT",
        priority=90,
        resources={"cpu_cores": 1, "ram_gib": 14.0, "gpu_vram_gib": 0.0},
    )
    _insert_task(
        db,
        task_id="SMALL",
        priority=10,
        resources={"cpu_cores": 1, "ram_gib": 2.0, "gpu_vram_gib": 0.0},
    )
    w = _insert_worker(db, "only", ram_gib=16.0, load={"ram_used_gib": 10.0})
    # First claim attempt refreshes hold; SMALL must not claim.
    r1 = _claim(db_path, w)
    assert r1.get("task") is None
    hold = get_remote_reservation()
    assert hold.get("key") == ("proj", "FAT")
    assert hold.get("worker_id") == "only"
    # Still blocked
    w = db.execute("SELECT * FROM workers WHERE worker_id='only'").fetchone()
    r2 = _claim(db_path, w)
    assert r2.get("task") is None
    assert db.execute("SELECT status FROM tasks WHERE task_id='SMALL'").fetchone()["status"] == "queued"


def test_shadow_mode_claims_despite_better_peer(tmp_path: Path):
    clear_remote_reservation()
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    set_config(db, "placement_mode", "binpack")
    set_config(db, "placement_shadow_mode", "1")
    set_config(db, "placement_age_sec", "3600")
    _insert_task(db, resources={"cpu_cores": 1, "ram_gib": 4.0, "gpu_vram_gib": 0.0})
    w_tight = _insert_worker(db, "tight", ram_gib=16.0, load={"ram_used_gib": 8.0})
    _insert_worker(db, "roomy", ram_gib=32.0, load={"ram_used_gib": 0.0})
    r = _claim(db_path, w_tight)
    assert r.get("task") is not None


def test_stale_peer_ignored(tmp_path: Path):
    """Unreachable/offline peers must not block claim (anti-starvation)."""
    clear_remote_reservation()
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    set_config(db, "placement_mode", "binpack")
    set_config(db, "placement_age_sec", "3600")
    _insert_task(db, resources={"cpu_cores": 1, "ram_gib": 4.0, "gpu_vram_gib": 0.0})
    w_tight = _insert_worker(db, "tight", ram_gib=16.0, load={"ram_used_gib": 0.0})
    # Roomy but stale last_seen → worker_health offline → not compared
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, "
        "tags, projects, status, last_seen, load_json) "
        "VALUES ('roomy', 'windows', 'x86_64', 64, 8, 0, '[]', ?, 'online', ?, '{}')",
        (json.dumps(["proj"]), old),
    )
    db.commit()
    r = _claim(db_path, w_tight)
    assert r.get("task") is not None


def test_prefer_last_worker_tie_break():
    from taskqueue.placement import claiming_worker_is_best_fit

    a = Node(node_id="a", ram_gib=16, ram_used_gib=0, cpu_cores=8, projects=["proj"])
    b = Node(node_id="b", ram_gib=16, ram_used_gib=0, cpu_cores=8, projects=["proj"])
    # Equal scores; preferred is b → a should defer
    ok, better = claiming_worker_is_best_fit(
        a, ram=4.0, gpu=0.0, peer_nodes=[b], preferred_node_id="b"
    )
    assert not ok and better is not None and better.node_id == "b"
    # Claiming preferred → ok
    ok2, _ = claiming_worker_is_best_fit(
        b, ram=4.0, gpu=0.0, peer_nodes=[a], preferred_node_id="b"
    )
    assert ok2


def test_remote_claim_records_estimate(tmp_path: Path):
    clear_remote_reservation()
    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    set_config(db, "placement_mode", "first_fit")
    _insert_task(db, resources={"cpu_cores": 1, "ram_gib": 7.5, "gpu_vram_gib": 0.0})
    w = _insert_worker(db, "w1", ram_gib=32.0)
    r = _claim(db_path, w)
    assert r.get("task")
    row = db.execute("SELECT estimated_ram_gib FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert row["estimated_ram_gib"] == pytest.approx(7.5)


def test_capacity_uses_live_load(tmp_path: Path):
    from taskqueue.capacity import build_capacity

    db_path = tmp_path / "q.db"
    db = open_db(db_path)
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, "
        "tags, projects, status, last_seen, load_json) "
        "VALUES ('w1', 'windows', 'x86_64', 16, 8, 0, '[]', '[]', 'online', ?, ?)",
        (now, json.dumps({"ram_used_gib": 10.0, "cpu_used": 2.0})),
    )
    db.commit()
    cap = build_capacity(db)
    w = next(x for x in cap["workers"] if x["worker_id"] == "w1")
    assert w["ram_gib_free"] == pytest.approx(6.0)
    assert w["cpu_cores_free"] == pytest.approx(6.0)
