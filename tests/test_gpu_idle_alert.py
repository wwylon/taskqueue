"""gpu_idle system alert: GPU tasks queued, none running, for GPU_IDLE_GRACE_SEC."""

from __future__ import annotations

import json

from tqdaemon import system_alerts as sa
from tqdaemon.db import open_db


def _task(db, tid, status, gpu=0.0, block=""):
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, args, enqueued_at, updated_at, resources, block_reason) "
        "VALUES ('p', ?, ?, 'x', '[]', '2026-01-01', '2026-01-01', ?, ?)",
        (tid, status, json.dumps({"gpu_vram_gib": gpu}), block),
    )
    db.commit()


def test_fires_after_grace_and_resets_when_gpu_runs(tmp_path, monkeypatch):
    db = open_db(tmp_path / "q.db")
    fired = []
    monkeypatch.setattr(sa, "maybe_emit_system_alert", lambda *a, **k: fired.append((a[2], a[3])) or True)
    monkeypatch.setattr(sa, "_gpu_idle_since", None)
    _task(db, "cpu", "queued")
    assert sa.evaluate_gpu_idle(db, None, mono=0.0) is False  # no GPU task queued
    _task(db, "g1", "queued", gpu=4, block="RAM: host 9/10 GiB")
    assert sa.evaluate_gpu_idle(db, None, mono=100.0) is False  # condition first seen
    assert sa.evaluate_gpu_idle(db, None, mono=100.0 + sa.GPU_IDLE_GRACE_SEC - 1) is False
    assert sa.evaluate_gpu_idle(db, None, mono=100.0 + sa.GPU_IDLE_GRACE_SEC) is True
    assert fired and fired[0][0] == "gpu_idle" and "p/g1" in fired[0][1] and "RAM: host" in fired[0][1]
    _task(db, "g2", "running", gpu=2)
    assert sa.evaluate_gpu_idle(db, None, mono=5000.0) is False and sa._gpu_idle_since is None


def test_key_registered_and_firing_state(tmp_path):
    assert "gpu_idle" in sa.SYSTEM_ALERT_KEYS and sa.SYSTEM_ALERT_LEVELS["gpu_idle"] == "amber"
    db = open_db(tmp_path / "q.db")
    _task(db, "g1", "queued", gpu=4)
    assert sa._gpu_idle_state(db)[0][0]["task"] == "p/g1"
