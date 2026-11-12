"""Regression tests for the Nov 2026 backend audit (Wave 1 correctness fixes)."""

from __future__ import annotations

from tqdaemon import daemon
from tqdaemon.models import STATUS_COMPLETED, STATUS_INTERRUPTED


class _Proc:
    def __init__(self, code_before_stop):
        self._code = code_before_stop

    def poll(self):
        return self._code


class _Run:
    """Minimal ActiveRun stand-in for shutdown finalization."""

    def __init__(self, code_before_stop, code_after_stop):
        self.proc = _Proc(code_before_stop)
        self._after = code_after_stop
        self._exit_code = code_before_stop
        self.stop_reason = None
        self.final = None

    def stop(self):
        self._exit_code = self._after  # e.g. a task that traps SIGTERM and exits 0
        self.proc._code = self._after

    def finalize(self, status, **_kw):
        self.final = status


def test_shutdown_never_marks_a_run_we_killed_as_completed():
    trapped = _Run(code_before_stop=None, code_after_stop=0)
    finished = _Run(code_before_stop=0, code_after_stop=0)
    crashed = _Run(code_before_stop=3, code_after_stop=3)
    daemon._stop_and_finalize_all({("p", "a"): trapped, ("p", "b"): finished, ("p", "c"): crashed})
    assert trapped.final == STATUS_INTERRUPTED
    assert finished.final == STATUS_COMPLETED
    assert crashed.final == STATUS_INTERRUPTED
    assert trapped.stop_reason == "daemon_shutdown"


def test_older_code_refuses_a_newer_schema_and_keeps_settings(tmp_path):
    import json

    import pytest

    from tqdaemon import migrations

    path = tmp_path / "queue.db"
    db = migrations.open_db(path)
    db.execute("INSERT OR REPLACE INTO config(key, value) VALUES ('future_knob', '1')")
    db.execute("UPDATE config SET value=? WHERE key='schema_version'", (json.dumps(migrations.SCHEMA_VERSION + 1),))
    db.commit()
    db.close()
    with pytest.raises(migrations.SchemaTooNewError):
        migrations.open_db(path)
    import sqlite3

    raw = sqlite3.connect(path)
    assert raw.execute("SELECT value FROM config WHERE key='future_knob'").fetchone() == ("1",)
    assert json.loads(raw.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()[0]) == (
        migrations.SCHEMA_VERSION + 1
    )
    raw.close()


def test_yield_master_expires_lease_and_backs_off(tmp_path, monkeypatch):
    from tqdaemon import lease, migrations

    db = migrations.open_db(tmp_path / "queue.db")
    assert lease._cas_master_lease(db, "me:1:a") is True
    epoch0 = db.execute("SELECT epoch FROM master_lease").fetchone()["epoch"]
    monkeypatch.setattr(lease, "_yield_until", 0.0)
    assert lease.yield_master_lease(db, "me:1:a") is True
    row = db.execute("SELECT holder, expires_at, epoch FROM master_lease").fetchone()
    assert row["expires_at"] == 0 and row["epoch"] == epoch0  # not deleted, epoch kept
    assert lease._cas_master_lease(db, "me:1:a") is False  # we do not grab it straight back
    monkeypatch.setattr(lease, "_yield_until", 0.0)
    assert lease._cas_master_lease(db, "me:1:a") is True
    assert db.execute("SELECT epoch FROM master_lease").fetchone()["epoch"] > epoch0


def test_null_resource_fields_do_not_break_admission_math():
    from tqdaemon.scheduler import _num, declared_resources

    assert _num(None, 1.0) == 1.0 and _num("x", 0.0) == 0.0 and _num("2.5", 0.0) == 2.5
    row = {"resources": '{"cpu_cores": null, "ram_gib": null, "gpu_vram_gib": 4}'}
    assert declared_resources(row) == (1.0, 0.0, 4.0)


def test_metric_keys_are_capped(monkeypatch):
    from tqdaemon import runner

    monkeypatch.setattr(runner, "MAX_METRIC_KEYS", 3)
    run = runner.ActiveRun.__new__(runner.ActiveRun)
    run.last_metrics = {}
    for i in range(5):
        run._merge_metrics({f"k{i}": i, "k0": 99})
    assert list(run.last_metrics) == ["k0", "k1", "k2"] and run.last_metrics["k0"] == 99
