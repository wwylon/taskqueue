"""Wave 0 deep-health regressions (F-192–F-200)."""

from __future__ import annotations

import threading
import time

from taskqueue import daemon
from taskqueue.arg_overrides import merge_arg_overrides, overrides_from_set_dict
from taskqueue.db import open_db
from taskqueue.resources import ResourceBudget
from taskqueue.statuses import FINALIZE_STATUSES
from taskqueue.waiting import wait_for


def test_merge_arg_overrides_preserves_base():
    merged = merge_arg_overrides(["--data", "x", "--epochs", "5"], ["--lr=0.1"])
    assert merged == ["--data", "x", "--epochs", "5", "--lr=0.1"]
    replaced = merge_arg_overrides(["--lr=0.01", "--epochs", "5"], ["--lr=0.1"])
    assert replaced == ["--lr=0.1", "--epochs", "5"]


def test_overrides_from_set_dict():
    assert "--lr=0.1" in overrides_from_set_dict({"--lr": 0.1})
    assert "--verbose" in overrides_from_set_dict({"--verbose": True})


def test_wait_for_empty_keys_not_ok():
    def open_conn():
        raise AssertionError("must not open DB for empty selection")

    result = wait_for(open_conn, [])
    assert result["ok"] is False
    assert "empty selection" in result.get("error", "")


def test_gpu_hold_credit_allows_pack_on_single_device(monkeypatch):
    monkeypatch.setattr("taskqueue.resources.ram_used_gib", lambda: 10.0)
    system = {
        "cpu_total_cores": 8,
        "ram_total_gib": 64.0,
        "gpu_vram_total_gib": 24.0,
        "gpus": [{"index": 0, "vram_gib": 24.0, "vram_used_gib": 0.0}],
    }
    cfg = {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": 80}
    budget = ResourceBudget(system, cfg)
    budget.reserve(0, 20.0, 8.0)  # P6 hold
    ok, reason = budget.fits(0, 20.0, 8.0, ram_hold_credit=8.0, gpu_hold_credit=20.0)
    assert ok, reason


def test_clock_jump_lease_lost_stamps_last_wall(tmp_path, monkeypatch):
    """F-193: lease-lost early return must stamp last_wall."""
    conn = open_db(tmp_path / "queue.db")
    env = daemon.TickEnv(
        conn=conn,
        home=tmp_path,
        active_runs={},
        system_info={"cpu_total_cores": 8, "ram_total_gib": 64.0},
        my_instance="me",
        sync_requested=threading.Event(),
        hw_refresh_now=threading.Event(),
        lease_lost=threading.Event(),
        is_shutdown=lambda: False,
        host_sleeping=lambda: False,
    )
    st = daemon.TickState({})
    st.last_wall = time.time() - 10_000
    env.lease_lost.set()
    monkeypatch.setattr(daemon, "_cas_master_lease", lambda *a: False)
    monkeypatch.setattr(daemon, "_enforce_hard_pressure", lambda *a, **k: None)
    before = time.time()
    assert daemon.daemon_tick(env, st) is False
    assert st.last_wall >= before - 1.0
    assert not daemon.is_clock_jump(st.last_wall, time.time(), 30.0)
    conn.close()


def test_worker_recovery_status_is_finalize_accepted():
    assert "interrupted" in FINALIZE_STATUSES
    assert "unknown" not in FINALIZE_STATUSES
