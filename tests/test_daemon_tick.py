"""daemon_tick: return contract, phase ordering, cross-tick state, lease fence, host sleep."""
import threading
import time

import pytest

from taskqueue import daemon, power, sched_wake
from taskqueue.db import open_db


@pytest.fixture
def env_st(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "queue.db")
    flags = {"sleeping": False, "shutdown": False}
    env = daemon.TickEnv(
        conn=conn, home=tmp_path, active_runs={}, system_info={"cpu_total_cores": 8},
        my_instance="me", sync_requested=threading.Event(),
        hw_refresh_now=threading.Event(), lease_lost=threading.Event(),
        is_shutdown=lambda: flags["shutdown"], host_sleeping=lambda: flags["sleeping"],
    )
    st = daemon.TickState({})
    calls = []
    monkeypatch.setattr(st.hw, "maybe_refresh", lambda *a, **k: None)
    monkeypatch.setattr(power, "set_prevent_sleep", lambda on: calls.append(("sleep", on)))
    monkeypatch.setattr(daemon, "_collect_finished_runs", lambda *a, **k: calls.append("reap"))
    monkeypatch.setattr(daemon, "_enforce_hard_pressure", lambda *a, **k: calls.append("pressure"))
    monkeypatch.setattr(daemon, "_preempt_scavenger", lambda *a, **k: calls.append("scav"))
    monkeypatch.setattr(
        daemon, "_run_local_admission", lambda *a, **k: calls.append("admit") or True
    )
    yield env, st, flags, calls
    conn.close()


def test_normal_tick_completes_in_order(env_st):
    env, st, _, calls = env_st
    assert daemon.daemon_tick(env, st) is True
    names = [c for c in calls if isinstance(c, str)]
    assert names == ["reap", "pressure", "scav", "admit"]
    assert st.cooldown_logged is True  # admission result stored
    assert st.interval == 5.0 and st.cfg
    assert env.system_info["cpu_used_cores_ema"] == st.cpu_ema


def test_host_asleep_reaps_but_does_not_admit(env_st):
    env, st, flags, calls = env_st
    flags["sleeping"] = True
    assert daemon.daemon_tick(env, st) is False
    assert "reap" in calls and "admit" not in calls and "pressure" not in calls


def test_lease_lost_not_reclaimed_skips_reap_and_admit(env_st, monkeypatch):
    env, st, _, calls = env_st
    env.lease_lost.set()
    monkeypatch.setattr(daemon, "_cas_master_lease", lambda *a: False)
    assert daemon.daemon_tick(env, st) is False
    assert calls == ["pressure"]  # hard pressure still enforced, nothing else
    assert env.lease_lost.is_set()


def test_lease_reclaimed_clears_flag_and_continues(env_st, monkeypatch):
    env, st, _, calls = env_st
    env.lease_lost.set()
    monkeypatch.setattr(daemon, "_cas_master_lease", lambda *a: True)
    assert daemon.daemon_tick(env, st) is True
    assert not env.lease_lost.is_set() and "admit" in calls


def test_lease_reclaim_error_returns_false(env_st, monkeypatch):
    env, st, _, calls = env_st

    def boom(*a):
        raise RuntimeError("locked")

    env.lease_lost.set()
    monkeypatch.setattr(daemon, "_cas_master_lease", boom)
    assert daemon.daemon_tick(env, st) is False
    assert "admit" not in calls


def test_hard_pressure_freeze_is_remembered(env_st, monkeypatch):
    env, st, _, _ = env_st
    monkeypatch.setattr(daemon, "_enforce_hard_pressure", lambda *a, **k: 12345.0)
    daemon.daemon_tick(env, st)
    assert st.admit_frozen_until == 12345.0


def test_clock_jump_detected_on_large_wall_gap(env_st, monkeypatch):
    env, st, _, _ = env_st
    seen = []
    monkeypatch.setattr(daemon, "_handle_clock_jump", lambda *a, **k: seen.append(k["gap"]))
    st.last_wall = time.time() - 10_000
    daemon.daemon_tick(env, st)
    assert len(seen) == 1 and seen[0] > 9_000
    assert time.time() - st.last_wall < 5  # stamped for the next tick


def test_no_clock_jump_on_normal_gap(env_st, monkeypatch):
    env, st, _, _ = env_st
    monkeypatch.setattr(daemon, "_handle_clock_jump", lambda *a, **k: pytest.fail("jump"))
    daemon.daemon_tick(env, st)


def test_idle_wait_stamps_wall_so_slow_tick_is_not_a_jump(env_st, monkeypatch):
    env, st, _, _ = env_st
    st.interval = 0.01
    st.last_wall = 1.0  # ancient: stale stamp from before a slow tick
    monkeypatch.setattr(sched_wake, "wait_or_timeout", lambda s: False)
    daemon.idle_wait_and_fast_admit(env, st)
    assert time.time() - st.last_wall < 5


def test_idle_wake_syncs_then_fast_admits(env_st, monkeypatch):
    env, st, _, _ = env_st
    order = []
    env.sync_requested.set()
    st.graceful_stop = 7
    monkeypatch.setattr(sched_wake, "wait_or_timeout", lambda s: True)
    monkeypatch.setattr(daemon, "load_projects", lambda home: order.append("load") or {})
    monkeypatch.setattr(
        daemon, "_fast_reap_and_admit",
        lambda *a, **k: order.append(("admit", a[6])) or False,
    )
    daemon.idle_wait_and_fast_admit(env, st)
    assert order == ["load", ("admit", 7)]
    assert not env.sync_requested.is_set()


def test_idle_wake_skipped_on_shutdown(env_st, monkeypatch):
    env, st, flags, _ = env_st
    flags["shutdown"] = True
    monkeypatch.setattr(sched_wake, "wait_or_timeout", lambda s: True)
    monkeypatch.setattr(daemon, "_fast_reap_and_admit", lambda *a, **k: pytest.fail("admit"))
    daemon.idle_wait_and_fast_admit(env, st)


def test_prevent_sleep_follows_active_runs(env_st):
    env, st, _, calls = env_st
    daemon.daemon_tick(env, st)
    assert ("sleep", False) in calls
