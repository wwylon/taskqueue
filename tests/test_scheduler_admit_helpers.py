"""Helpers split out of _local_admit_queued: excess-running stops, estimate cache, P6 hold."""
from taskqueue import _db_tasks, scheduler


def _rows(*pairs):
    return [{"project": p, "task_id": t} for p, t in pairs]


def _capture_stops(monkeypatch):
    stops = []
    monkeypatch.setattr(
        _db_tasks, "request_stop_with_control", lambda c, h, p, t: stops.append((p, t))
    )
    import taskqueue.db as db

    monkeypatch.setattr(db, "request_stop_with_control", lambda c, h, p, t: stops.append((p, t)))
    monkeypatch.setattr(scheduler, "_most_recent_first", lambda conn, rows: list(reversed(rows)))
    monkeypatch.setattr(scheduler, "get_project_limits", lambda c, p: {})
    return stops


def test_global_cap_stops_newest_local_only(monkeypatch):
    stops = _capture_stops(monkeypatch)
    snap = _rows(("a", "1"), ("a", "2"), ("a", "3"))
    active = {("a", "2"): 1, ("a", "3"): 1}  # a/1 is remote
    scheduler._enforce_excess_running(None, None, active, snap, 2, 3, {"a": 3})
    assert stops == [("a", "3")]


def test_no_cap_no_stops(monkeypatch):
    stops = _capture_stops(monkeypatch)
    snap = _rows(("a", "1"), ("a", "2"))
    scheduler._enforce_excess_running(None, None, {("a", "1"): 1}, snap, 0, 2, {"a": 2})
    assert stops == []


def test_project_cap_stops_excess(monkeypatch):
    stops = _capture_stops(monkeypatch)
    monkeypatch.setattr(scheduler, "get_project_limits", lambda c, p: {"max_tasks": 1})
    snap = _rows(("a", "1"), ("a", "2"))
    active = {("a", "1"): 1, ("a", "2"): 1}
    scheduler._enforce_excess_running(None, None, active, snap, 0, 2, {"a": 2})
    assert stops == [("a", "2")]


def test_stop_failure_is_swallowed(monkeypatch, capsys):
    import taskqueue.db as db

    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(db, "request_stop_with_control", boom)
    monkeypatch.setattr(scheduler, "_most_recent_first", lambda conn, rows: list(rows))
    monkeypatch.setattr(scheduler, "get_project_limits", lambda c, p: {})
    scheduler._enforce_excess_running(
        None, None, {("a", "1"): 1, ("a", "2"): 1}, _rows(("a", "1"), ("a", "2")), 1, 2, {}
    )
    assert "excess-stop" in capsys.readouterr().err


def test_estimate_cache_evicts_only_changed_tasks(tmp_path, monkeypatch):
    from taskqueue.db import open_db

    conn = open_db(tmp_path / "q.db")
    monkeypatch.setattr(scheduler, "_estimate_cross_tick", {})
    monkeypatch.setattr(scheduler, "_estimate_gen", -1)
    cache = scheduler._shared_estimate_cache(conn)
    cache[("p", "t1", 5)] = {"source": "history"}
    cache[("p", "t2", 5)] = {"source": "history"}
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO runs (project, task_id, status, started_at) VALUES ('p','t2','completed',?)",
        (now,),
    )
    conn.commit()
    monkeypatch.setattr(scheduler, "_estimate_gen", (0, ""))
    cache2 = scheduler._shared_estimate_cache(conn)
    assert ("p", "t1", 5) in cache2 and ("p", "t2", 5) not in cache2
    conn.close()


def test_estimate_cache_evicts_when_a_run_finishes(tmp_path, monkeypatch):
    from taskqueue.db import open_db

    conn = open_db(tmp_path / "q.db")
    conn.execute(
        "INSERT INTO runs (project, task_id, status, started_at) VALUES ('p','t1','running','2026-01-01T00:00:00Z')"
    )
    conn.commit()
    monkeypatch.setattr(scheduler, "_estimate_cross_tick", {})
    monkeypatch.setattr(scheduler, "_estimate_gen", -1)
    cache = scheduler._shared_estimate_cache(conn)
    cache[("p", "t1", 5)] = {"source": "history"}
    cache[("q", "s", 5)] = {"source": "sibling"}
    cache[("q", "h", 5)] = {"source": "history"}
    # Same MAX(run_id): only finished_at changes.
    conn.execute("UPDATE runs SET status='completed', finished_at='2026-01-01T01:00:00Z', peak_ram_gib=9")
    conn.commit()
    cache2 = scheduler._shared_estimate_cache(conn)
    assert ("p", "t1", 5) not in cache2  # its history changed
    assert ("q", "s", 5) not in cache2  # sibling estimates borrow other tasks' history
    assert ("q", "h", 5) in cache2
    conn.close()


def test_p6_no_top_returns_empty_hold(monkeypatch):
    monkeypatch.setattr(scheduler, "pick_reservation_top", lambda *a: None)
    state = {"key": ("p", "t")}
    top, key, ram, vram = scheduler._p6_reserve_top(
        None, [object()], None, 60, state, None, {}, {}, None, 1.0, 5
    )
    assert (top, key, ram, vram) == (None, None, 0.0, 0.0) and state == {}


def test_p6_disabled_never_picks(monkeypatch):
    monkeypatch.setattr(
        scheduler, "pick_reservation_top", lambda *a: (_ for _ in ()).throw(AssertionError)
    )
    top, key, *_ = scheduler._p6_reserve_top(None, [object()], None, 0, {}, None, {}, {}, None, 1.0, 5)
    assert top is None and key is None
