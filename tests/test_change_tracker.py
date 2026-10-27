"""ChangeTracker: counter bumps only when another connection commits."""

import sqlite3

from taskqueue.web.change_tracker import ChangeTracker


def test_counter_changes_on_foreign_commit(tmp_path):
    db = tmp_path / "t.db"
    w = sqlite3.connect(db)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("CREATE TABLE x(a)")
    w.commit()
    t = ChangeTracker(db, min_interval=0)
    base = t.token()
    assert t.token() == base  # idle: unchanged
    w.execute("INSERT INTO x VALUES (1)")
    w.commit()
    assert t.token() == base + 1
    assert t.token() == base + 1


def test_throttle_reuses_value(tmp_path):
    db = tmp_path / "t.db"
    w = sqlite3.connect(db)
    w.execute("CREATE TABLE x(a)")
    w.commit()
    t = ChangeTracker(db, min_interval=60)
    base = t.token()
    w.execute("INSERT INTO x VALUES (1)")
    w.commit()
    assert t.token() == base  # inside throttle window


def test_unavailable_returns_none(tmp_path):
    t = ChangeTracker(tmp_path / "missing" / "no.db", min_interval=0)
    assert t.token() is None
