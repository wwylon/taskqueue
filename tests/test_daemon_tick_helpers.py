from types import SimpleNamespace

from taskqueue import daemon


class FakeRun:
    def __init__(self, elapsed=0.0):
        self.stop_reason = None
        self.stopped = False
        self.coop = 0
        self._elapsed = elapsed

    def request_cooperative_suspend(self, home):
        self.coop += 1

    def suspend_grace_elapsed(self, grace):
        return grace <= 0

    def stop(self, *, background=False):
        self.stopped = True

    def elapsed_sec(self):
        return self._elapsed


def _patch_task(monkeypatch, row):
    monkeypatch.setattr(daemon, "get_task", lambda conn, p, t: row)


class _Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


class _Conn:
    def execute(self, *a, **k):
        return SimpleNamespace(fetchone=lambda: None)


def test_stop_request_cooperative_then_stop(monkeypatch):
    _patch_task(monkeypatch, _Row(stop_requested=1, wall_clock_cap_sec=None))
    run = FakeRun()
    runs = {("p", "t"): run}
    daemon._enforce_stops_and_caps(_Conn(), runs, None, 10.0)
    assert run.stop_reason == "stop_requested" and run.coop == 1 and not run.stopped
    daemon._enforce_stops_and_caps(_Conn(), runs, None, 0.0)
    assert run.stopped


def test_wall_clock_cap(monkeypatch):
    _patch_task(monkeypatch, _Row(stop_requested=0, wall_clock_cap_sec=5))
    import taskqueue.db as db
    seen = []
    monkeypatch.setattr(db, "request_stop", lambda conn, p, t: seen.append((p, t)))
    run = FakeRun(elapsed=9)
    daemon._enforce_stops_and_caps(_Conn(), {("p", "t"): run}, None, 0.0)
    assert run.stop_reason == "wall_clock_cap" and run.stopped and seen == [("p", "t")]


def test_clock_jump_stops_and_clears(monkeypatch):
    monkeypatch.setattr(daemon, "get_config", lambda conn: {"graceful_stop_sec": 3})
    calls = []
    monkeypatch.setattr(daemon, "requeue_orphaned_running", lambda conn, **kw: calls.append(kw))
    run = FakeRun()
    runs = {("p", "t"): run}
    daemon._handle_clock_jump(object(), runs, gap=100.0, jump_thr=60.0)
    assert run.stopped and runs == {} and calls[0]["reason"] == "clock-jump"


class PressureRun:
    def __init__(self, ram=0.0, vram=0.0):
        self.current_ram_gib = ram
        self.current_vram_gib = vram
        self.stop_reason = None
        self.stopped = False
        self.run_id = 1

    def update_resources(self):
        pass

    def stop(self, *, background=False):
        self.stopped = True


def test_hard_ram_pressure_stops_victim_and_freezes_admission(monkeypatch):
    run = PressureRun(ram=30.0)
    runs = {("p", "t"): run}
    monkeypatch.setattr(daemon, "ram_used_gib", lambda: 98.0)
    monkeypatch.setattr(daemon, "_split_dying", lambda ar, attr: (dict(ar), 0.0))
    monkeypatch.setattr(
        daemon, "pick_victim",
        lambda conn, items, needed_gib, resource: (items[0][0], [(items[0][0], 1.0)]),
    )
    errs = []
    monkeypatch.setattr(daemon, "set_task_error", lambda *a, **k: errs.append(a[1:4]))
    cfg = {"ram_hard_limit_pct": 95, "ram_limit_pct": 80, "recovery_cooldown_sec": 30}
    out = daemon._enforce_hard_pressure(object(), cfg, {"ram_total_gib": 100.0}, runs, 1000.0)
    assert out == 1030.0
    assert run.stopped and run.stop_reason == "ram_hard" and errs


def test_no_pressure_returns_none(monkeypatch):
    monkeypatch.setattr(daemon, "ram_used_gib", lambda: 10.0)
    out = daemon._enforce_hard_pressure(
        object(), {}, {"ram_total_gib": 100.0}, {("p", "t"): PressureRun(ram=1.0)}, 5.0
    )
    assert out is None


class HbRun:
    def __init__(self, idle):
        self.task_row = {"resources": "{}", "command": "python"}
        self._idle = idle
        self.peak_vram_gib = 0.0
        self.gpu_util_ema = None
        self._gpu_util_samples = 0
        self._rss_leak_warned = False
        self.current_ram_gib = 1.0
        self._sanity_prev_pct = None
        self._sanity_prev_active = 0.0
        self.progress_pct = None
        self.run_id = 7
        self.stop_reason = None
        self.stopped = False
        self.db_updates = 0

    def update_db_progress(self, conn):
        self.db_updates += 1

    def refresh_active_cpu(self):
        return 0.0

    def elapsed_sec(self):
        return 5000.0

    def idle_sec(self):
        return self._idle

    def active_idle_sec(self):
        return self._idle

    def rss_growth_rate_gib_per_hour(self):
        return None

    def stop(self, *, background=False):
        self.stopped = True


def test_heartbeat_frozen_run_stopped_when_configured(monkeypatch):
    notes = []
    monkeypatch.setattr(daemon, "_note_block", lambda *a, **k: notes.append(a[1:4]))
    run = HbRun(idle=10_000.0)
    runs = {("p", "t"): run}
    daemon._heartbeat_and_sanity(
        object(), None, {"frozen_action": "stop"}, runs, 600.0, set(1 for _ in ())
    )
    assert run.db_updates == 1
    assert run.stopped and run.stop_reason == "frozen"
    assert notes


def test_heartbeat_healthy_run_left_alone(monkeypatch):
    monkeypatch.setattr(daemon, "_note_block", lambda *a, **k: None)
    run = HbRun(idle=1.0)
    daemon._heartbeat_and_sanity(object(), None, {}, {("p", "t"): run}, 600.0, set())
    assert not run.stopped and run.db_updates == 1


class UsageRun:
    current_vram_gib = 2.0
    task_row = {"started_by": "alice"}

    def drain_cpu_seconds(self):
        return 3.0


def _usage_conn():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE project_usage (project TEXT PRIMARY KEY, gpu_seconds REAL, cpu_seconds REAL, updated_at INT)"
    )
    c.execute(
        "CREATE TABLE usage_snapshot (ts INT, project TEXT, operator TEXT, cpu_seconds REAL, gpu_seconds REAL,"
        " PRIMARY KEY (ts, project, operator))"
    )
    return c


def test_usage_accumulate_and_flush():
    u = daemon._UsageAccumulator()
    u.accumulate({("p", "t"): UsageRun()}, 5.0)
    assert u.proj_gpu == {"p": 10.0} and u.proj_cpu == {"p": 3.0}
    assert u.op_usage[("p", "alice")] == {"gpu_seconds": 10.0, "cpu_seconds": 3.0}
    conn = _usage_conn()
    u.flush_if_due(conn, {}, 100.0)
    row = conn.execute("SELECT gpu_seconds, cpu_seconds FROM project_usage WHERE project='p'").fetchone()
    assert (row[0], row[1]) == (10.0, 3.0)
    assert conn.execute("SELECT COUNT(*) FROM usage_snapshot").fetchone()[0] == 1
    assert u.proj_gpu == {} and u.op_usage == {}


def test_usage_skipped_while_host_sleeping():
    u = daemon._UsageAccumulator()
    u.accumulate({("p", "t"): UsageRun()}, 5.0, host_sleeping=True)
    assert u.proj_gpu == {}


def test_hardware_refresher_updates_in_place_and_backs_off(monkeypatch):
    import threading
    calls = []

    def fake_detect():
        calls.append(1)
        return {"ram_total_gib": 64.0, "gpu_probe_error": "boom", "gpus": []}

    monkeypatch.setattr(daemon, "detect_system", fake_detect)
    info = {"stale": 1, "cpu_used_cores_ema": 2.0}
    ev = threading.Event()
    h = daemon._HardwareRefresher()
    assert h.maybe_refresh(info, 1000.0, ev) is True
    assert "stale" not in info and info["cpu_used_cores_ema"] == 2.0
    assert info["ram_total_gib"] == 64.0 and h.fail_streak == 1
    # not due yet
    assert h.maybe_refresh(info, 1000.5, ev) is False and len(calls) == 1
    # forced refresh
    ev.set()
    assert h.maybe_refresh(info, 1000.6, ev) is True and not ev.is_set()
    assert len(calls) == 2 and h.fail_streak == 2


def test_heartbeat_line_only_on_progress_change_or_every_few_minutes(monkeypatch, capsys):
    monkeypatch.setattr(daemon, "_note_block", lambda *a, **k: None)
    run = HbRun(idle=1.0)
    runs = {("p", "t"): run}
    beat = lambda: daemon._heartbeat_and_sanity(object(), None, {}, runs, 600.0, set())  # noqa: E731
    beat(); beat(); beat()
    assert capsys.readouterr().out.count(">> p/t") == 1   # unchanged progress: one line, not three
    run.progress_pct = 42.0
    beat()
    assert capsys.readouterr().out.count(">> p/t") == 1   # progress moved: logged again
    run._hb_last_print -= daemon.HEARTBEAT_LOG_SEC
    beat()
    assert capsys.readouterr().out.count(">> p/t") == 1   # periodic line after HEARTBEAT_LOG_SEC


def test_registry_reload_is_visible_through_the_web_reference():
    registry = {"a": object()}
    web_ref = lambda: registry  # noqa: E731 - what start_web_server receives
    daemon._replace_registry(registry, {"a": object(), "new": object()})
    assert set(web_ref()) == {"a", "new"}


def test_stop_request_logs_requester_once(monkeypatch, tmp_path, capsys):
    from taskqueue.db import write_control_action

    write_control_action(tmp_path, "p", "t", "suspend", requested_by="ada via cli")
    assert daemon._control_requester(tmp_path, "p", "t") == "ada via cli"
    write_control_action(tmp_path, "p", "u", "suspend")
    assert daemon._control_requester(tmp_path, "p", "u") is None
