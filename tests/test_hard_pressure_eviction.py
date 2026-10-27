"""Hard RAM/VRAM eviction: caps, dying runs, VRAM path, no-victim case."""
from taskqueue import daemon
from tests.test_daemon_tick_helpers import PressureRun


def _patch(monkeypatch, *, host_used=98.0):
    monkeypatch.setattr(daemon, "ram_used_gib", lambda: host_used)
    errs = []
    monkeypatch.setattr(daemon, "set_task_error", lambda *a, **k: errs.append((a[1:3], a[3])))

    def pick(conn, items, needed_gib, resource):
        # biggest consumer of the relevant resource first
        attr = "current_vram_gib" if resource == "vram" else "current_ram_gib"
        items = sorted(items, key=lambda kv: -getattr(kv[1], attr))
        return items[0][0], [(k, 1.0) for k, _ in items]

    monkeypatch.setattr(daemon, "pick_victim", pick)
    return errs


def test_ram_stop_cap_limits_victims_per_tick(monkeypatch):
    errs = _patch(monkeypatch)
    runs = {("p", f"t{i}"): PressureRun(ram=20.0) for i in range(4)}
    cfg = {"ram_hard_limit_pct": 95, "ram_limit_pct": 50, "stop_max_per_tick": 2}
    daemon._enforce_hard_pressure(object(), cfg, {"ram_total_gib": 100.0}, runs, 0.0)
    assert sum(r.stopped for r in runs.values()) == 2
    assert len(errs) == 2


def test_ram_stops_only_until_under_soft_limit(monkeypatch):
    _patch(monkeypatch, host_used=98.0)
    big, small = PressureRun(ram=30.0), PressureRun(ram=1.0)
    runs = {("p", "big"): big, ("p", "small"): small}
    cfg = {"ram_hard_limit_pct": 95, "ram_limit_pct": 80, "stop_max_per_tick": 5}
    daemon._enforce_hard_pressure(object(), cfg, {"ram_total_gib": 100.0}, runs, 0.0)
    assert big.stopped and not small.stopped  # 98-30=68 < 80 soft limit


def test_dying_run_counts_as_relief_and_is_not_restopped(monkeypatch):
    errs = _patch(monkeypatch, host_used=98.0)
    dying = PressureRun(ram=30.0)
    dying.stop_reason = "ram_hard"
    live = PressureRun(ram=5.0)
    runs = {("p", "dying"): dying, ("p", "live"): live}
    cfg = {"ram_hard_limit_pct": 95, "ram_limit_pct": 80, "stop_max_per_tick": 5}
    out = daemon._enforce_hard_pressure(object(), cfg, {"ram_total_gib": 100.0}, runs, 0.0)
    assert not dying.stopped and not live.stopped and errs == []
    assert out is None


def test_no_victim_returns_none(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(daemon, "pick_victim", lambda *a, **k: (None, []))
    run = PressureRun(ram=30.0)
    cfg = {"ram_hard_limit_pct": 95, "ram_limit_pct": 80}
    out = daemon._enforce_hard_pressure(
        object(), cfg, {"ram_total_gib": 100.0}, {("p", "t"): run}, 0.0
    )
    assert out is None and not run.stopped


def test_vram_pressure_stops_biggest_and_uses_vram_error(monkeypatch):
    errs = _patch(monkeypatch, host_used=1.0)  # RAM fine
    big, small = PressureRun(vram=20.0), PressureRun(vram=2.0)
    runs = {("p", "big"): big, ("p", "small"): small}
    cfg = {"vram_hard_limit_pct": 90, "gpu_limit_pct": 80, "recovery_cooldown_sec": 10}
    info = {"ram_total_gib": 100.0, "gpu_vram_total_gib": 24.0}
    out = daemon._enforce_hard_pressure(object(), cfg, info, runs, 100.0)
    assert big.stopped and big.stop_reason == "vram_hard" and not small.stopped
    assert errs[0][0] == ("p", "big")
    assert errs[0][1] == daemon.tqerr.STOP_VRAM
    assert out == 110.0


def test_vram_below_hard_limit_is_left_alone(monkeypatch):
    errs = _patch(monkeypatch, host_used=1.0)
    run = PressureRun(vram=10.0)
    cfg = {"vram_hard_limit_pct": 90}
    info = {"ram_total_gib": 100.0, "gpu_vram_total_gib": 24.0}
    out = daemon._enforce_hard_pressure(object(), cfg, info, {("p", "t"): run}, 0.0)
    assert out is None and not run.stopped and errs == []
