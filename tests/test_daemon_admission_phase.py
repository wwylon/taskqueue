"""Daemon tick admission phase: cooldown freeze, CPU gate, reap-before-admit ordering."""
from taskqueue import daemon
import taskqueue.scheduler as scheduler


def _capture(monkeypatch):
    calls = []

    def fake_admit(conn, home, projects, active, info, cfg, grace, shutdown, **kw):
        calls.append(kw)

    monkeypatch.setattr(scheduler, "_local_admit_queued", fake_admit)
    return calls


def _run(now=100.0, frozen_until=0.0, logged=False, cpu_ema=0.0, cfg=None):
    return daemon._run_local_admission(
        object(), None, {}, {}, {"cpu_total_cores": 8}, cfg or {"cpu_limit_pct": 50},
        3.0, False,
        now=now, admit_frozen_until=frozen_until, cooldown_logged=logged,
        reservation_state={}, cpu_ema=cpu_ema,
    )


def test_no_gates_admits_unfrozen(monkeypatch):
    calls = _capture(monkeypatch)
    assert _run() is False
    assert calls[0]["admit_frozen"] is False
    assert calls[0]["host_cpu_blocked"] is False


def test_cooldown_freezes_admission_and_logs_once(monkeypatch, capsys):
    calls = _capture(monkeypatch)
    logged = _run(now=100.0, frozen_until=130.0)
    assert logged is True
    assert calls[0]["admit_frozen"] is True and "frozen" in calls[0]["admit_frozen_reason"]
    again = _run(now=101.0, frozen_until=130.0, logged=True)
    assert again is True
    out = capsys.readouterr().out
    assert out.count("admission frozen") == 1


def test_cooldown_expiry_resets_logged_flag(monkeypatch):
    _capture(monkeypatch)
    assert _run(now=200.0, frozen_until=130.0, logged=True) is False


def test_cpu_over_limit_blocks_with_reason(monkeypatch):
    calls = _capture(monkeypatch)
    _run(cpu_ema=6.0)  # limit = 8 * 50% = 4 cores
    assert calls[0]["host_cpu_blocked"] is True
    assert "CPU" in calls[0]["host_cpu_reason"]


def test_fast_reap_collects_before_admitting(monkeypatch):
    order = []
    monkeypatch.setattr(daemon, "_collect_finished_runs", lambda *a, **k: order.append("reap"))
    monkeypatch.setattr(scheduler, "_local_admit_queued", lambda *a, **k: order.append("admit"))
    daemon._fast_reap_and_admit(
        object(), None, {}, {}, {"cpu_total_cores": 8}, {}, 3.0, False,
        admit_frozen_until=0.0, cooldown_logged=False, reservation_state={}, cpu_ema=0.0,
    )
    assert order == ["reap", "admit"]
