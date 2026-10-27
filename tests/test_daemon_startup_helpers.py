"""Extracted run_daemon pieces: lease acquisition and the renew thread."""
from __future__ import annotations

import threading
import time

from taskqueue import daemon
from taskqueue.db import open_db


def test_acquire_master_lease_force_claim_bumps_epoch(tmp_path):
    conn = open_db(tmp_path / "q.db")
    conn.execute("INSERT OR REPLACE INTO master_lease (singleton, holder, expires_at, epoch) VALUES (1, 'old', 9999999999, 4)")
    conn.commit()
    daemon._acquire_master_lease(conn, "me", True, threading.Event())
    row = conn.execute("SELECT holder, epoch FROM master_lease WHERE singleton=1").fetchone()
    assert (row["holder"], row["epoch"]) == ("me", 5)


def test_acquire_master_lease_stops_when_event_set(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "q.db")
    monkeypatch.setattr(daemon, "_cas_master_lease", lambda *a: False)
    stop = threading.Event()
    stop.set()
    daemon._acquire_master_lease(conn, "me", False, stop)  # returns without looping


def test_lease_renew_thread_flags_loss(tmp_path, monkeypatch):
    path = tmp_path / "q.db"
    conn = open_db(path)
    conn.execute("INSERT OR REPLACE INTO master_lease (singleton, holder, expires_at, epoch) VALUES (1, 'other', 1, 1)")
    conn.commit()
    import taskqueue.config as cfg
    monkeypatch.setattr(cfg, "MASTER_LEASE_RENEW_SEC", 0.05)
    lost = threading.Event()
    stop = threading.Event()
    t = daemon._start_lease_renew_thread(path, "me", lost, stop.is_set)
    deadline = time.time() + 5
    while not lost.is_set() and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    t.join(2)
    assert lost.is_set()


def test_host_sleep_state_defaults():
    s = daemon._HostSleepState()
    assert s.sleeping is False and s.suspended_at == 0.0
