"""F-074: longer master lease + long-poll does not claim on empty queue."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from unittest.mock import patch

from taskqueue.config import MASTER_LEASE_RENEW_SEC, MASTER_LEASE_TTL_SEC, WORK_LONGPOLL_MAX_SEC
from taskqueue.daemon import _cas_master_lease
from taskqueue.db import (
    master_holder_process_dead,
    open_db,
    parse_master_instance_id,
    release_master_lease,
)
from tests.helpers.master_worker import (
    http_request,
    make_handler_env,
    seed_master_lease,
    seed_worker,
)


def test_master_lease_ttl_defaults() -> None:
    assert MASTER_LEASE_TTL_SEC == 600
    assert MASTER_LEASE_RENEW_SEC == 60
    assert MASTER_LEASE_TTL_SEC > WORK_LONGPOLL_MAX_SEC
    assert MASTER_LEASE_TTL_SEC >= 5 * MASTER_LEASE_RENEW_SEC


def test_idle_longpoll_skips_claim_when_queue_empty(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_worker(db, projects=[], session_id=None)
    with patch("taskqueue.web._claim_next_remote_task") as claim:
        claim.return_value = {"task": None}
        t0 = time.monotonic()
        code, data = http_request(
            HandlerClass, "GET", "/api/worker/next-task?worker_id=w1&wait=0.6"
        )
        elapsed = time.monotonic() - t0
    assert code == 200
    assert data["task"] is None
    assert elapsed >= 0.55
    assert claim.call_count == 0
    db.close()


def test_get_workers_includes_health(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_worker(db, projects=[], session_id=None)
    code, workers = http_request(HandlerClass, "GET", "/api/workers")
    assert code == 200
    assert len(workers) == 1
    assert workers[0]["health"] in ("online", "unreachable", "offline")
    db.close()


def test_parse_master_instance_id() -> None:
    assert parse_master_instance_id("dell:123:abcdef") == ("dell", 123, "abcdef")
    assert parse_master_instance_id("bad") is None
    assert parse_master_instance_id("h:x:y") is None


def test_master_holder_process_dead_other_host() -> None:
    assert master_holder_process_dead("other-host:1:abc") is False


def test_cas_reclaims_dead_same_host_holder(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    host = socket.gethostname()
    stale = f"{host}:424242:deadbeefdeadbeefdeadbeefdeadbeef"
    db.execute(
        "INSERT INTO master_lease (singleton, holder, expires_at, epoch) VALUES (1, ?, ?, 3)",
        (stale, int(time.time()) + 600),
    )
    db.commit()
    me = f"{host}:{os.getpid()}:{'c' * 32}"
    with patch("psutil.pid_exists", return_value=False):
        assert _cas_master_lease(db, me, ttl=90) is True
    row = db.execute("SELECT holder, epoch FROM master_lease WHERE singleton=1").fetchone()
    assert row["holder"] == me
    assert int(row["epoch"]) == 4
    db.close()


def test_cas_does_not_reclaim_live_same_host_holder(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    host = socket.gethostname()
    live = f"{host}:{os.getpid()}:{'d' * 32}"
    db.execute(
        "INSERT INTO master_lease (singleton, holder, expires_at, epoch) VALUES (1, ?, ?, 1)",
        (live, int(time.time()) + 600),
    )
    db.commit()
    other = f"{host}:{os.getpid() + 1}:{'e' * 32}"
    with patch("psutil.pid_exists", return_value=True):
        assert _cas_master_lease(db, other, ttl=90) is False
    row = db.execute("SELECT holder FROM master_lease WHERE singleton=1").fetchone()
    assert row["holder"] == live
    db.close()


def test_cas_does_not_reclaim_other_host_even_if_pid_missing(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    stale = "remote-box:424242:deadbeefdeadbeefdeadbeefdeadbeef"
    db.execute(
        "INSERT INTO master_lease (singleton, holder, expires_at, epoch) VALUES (1, ?, ?, 1)",
        (stale, int(time.time()) + 600),
    )
    db.commit()
    me = f"{socket.gethostname()}:{os.getpid()}:{'f' * 32}"
    with patch("psutil.pid_exists", return_value=False):
        assert _cas_master_lease(db, me, ttl=90) is False
    db.close()


def test_release_master_lease(tmp_path: Path) -> None:
    db = open_db(tmp_path / "q.db")
    holder = "dell:1:abc"
    seed_master_lease(db, holder=holder, ttl=600)
    assert release_master_lease(db, "other") is False
    assert release_master_lease(db, holder) is True
    assert db.execute("SELECT 1 FROM master_lease WHERE singleton=1").fetchone() is None
    db.close()


def test_get_workers_hides_secrets(tmp_path: Path) -> None:
    HandlerClass, db, _ = make_handler_env(tmp_path)
    seed_worker(db, projects=[], session_id="secret-sess")
    db.execute("UPDATE workers SET credential_hash='deadbeef'")
    db.commit()
    import taskqueue.web.routes_read as rr

    rr._workers_cache = (0.0, [])
    code, workers = http_request(HandlerClass, "GET", "/api/workers")
    assert code == 200 and workers
    for w in workers:
        assert "credential_hash" not in w and "session_id" not in w and "instance_id" not in w
    db.close()
