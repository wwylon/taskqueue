"""W7.5 worker self-upgrade: verify download hash, install, restart; abort on mismatch."""
from __future__ import annotations

import hashlib
import io
import subprocess
from pathlib import Path

from taskqueue import worker


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _setup(monkeypatch, payload: bytes, sha: str, installs: list, restarts: list) -> None:
    monkeypatch.setattr(worker, "_WORKER_VERSION", "1.0.0")
    monkeypatch.setattr(worker, "_get", lambda url: {"version": "1.2.0", "upgrade_url": "/api/worker/wheel", "sha256": sha})
    monkeypatch.setattr(worker.urllib.request, "urlopen", lambda req, timeout=0: _Resp(payload))
    monkeypatch.setattr(worker.subprocess, "run", lambda cmd, **k: installs.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(worker, "_restart_process", lambda: restarts.append(True))


def test_upgrade_installs_verified_wheel_then_restarts(tmp_path: Path, monkeypatch) -> None:
    payload = b"wheel-bytes"
    installs, restarts = [], []
    _setup(monkeypatch, payload, hashlib.sha256(payload).hexdigest(), installs, restarts)
    assert worker._check_worker_upgrade("http://m", tmp_path) is True
    assert (tmp_path / "wheels" / "worker.whl").read_bytes() == payload
    assert installs and restarts == [True]


def test_upgrade_aborts_on_hash_mismatch_and_while_busy(tmp_path: Path, monkeypatch) -> None:
    installs, restarts = [], []
    _setup(monkeypatch, b"tampered", "0" * 64, installs, restarts)
    assert worker._check_worker_upgrade("http://m", tmp_path) is False
    assert worker._check_worker_upgrade("http://m", tmp_path, active_run=True) is False
    assert worker._check_worker_upgrade("http://m", tmp_path, pin_version=True) is False
    assert installs == [] and restarts == []
    assert not (tmp_path / "wheels" / "worker_new.whl").exists()


def test_journal_recovery_queues_finalize_for_unfinished_runs(tmp_path: Path) -> None:
    import json

    from taskqueue.worker_journal import append_entry, journal_path

    append_entry(tmp_path, 5, {"t": "claim", "run_id": 5})
    append_entry(tmp_path, 5, {"t": "exit", "code": 0})          # exited, finalize never sent
    append_entry(tmp_path, 6, {"t": "claim", "run_id": 6})        # crashed mid-run
    worker._recover_journal(tmp_path, "w1")
    pending = {int(p.stem): json.loads(p.read_text(encoding="utf-8")) for p in worker.pending_finalize_dir(tmp_path).glob("*.json")}
    assert pending[5]["status"] == "completed" and pending[5]["exit_code"] == 0
    assert pending[6]["status"] == "unknown" and pending[6]["exit_code"] == -1
    assert not journal_path(tmp_path, 5).exists()  # replayed journal is collected
