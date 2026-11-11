"""Cooperative hold: control file + client maybe_hold."""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.helpers.master_worker import http_request, make_handler_env, seed_queued_task
from tqdaemon._db_tasks import clear_control_file, write_control_action
from tqdaemon.config import control_path


def test_write_control_hold_payload(tmp_path: Path) -> None:
    path = write_control_action(tmp_path, "p", "t", "hold", duration_sec=120, reason="cooling")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["action"] == "hold"
    assert data["reason"] == "cooling"
    assert data["duration_sec"] == 120
    assert float(data["until"]) > time.time()
    clear_control_file(tmp_path, "p", "t")
    assert not control_path(tmp_path, "p", "t").is_file()


def test_http_hold_and_unhold(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    seed_queued_task(db, project="p", task_id="t", dispatch="local")
    db.execute("UPDATE tasks SET status='running'")
    db.commit()
    status, body = http_request(H, "POST", "/api/hold/p/t?duration_sec=90&reason=gate")
    assert status == 200 and body["ok"] is True
    home = Path(H._db_path).parent
    data = json.loads(control_path(home, "p", "t").read_text(encoding="utf-8"))
    assert data["action"] == "hold" and data.get("reason") == "gate"
    status, body = http_request(H, "POST", "/api/unhold/p/t")
    assert status == 200 and body["ok"] is True
    assert not control_path(home, "p", "t").is_file()


def test_maybe_hold_respects_until(tmp_path: Path, monkeypatch) -> None:
    # Load the client module file directly (clients/ is not an installed package).
    import importlib.util

    client_path = Path(__file__).resolve().parents[1] / "clients" / "python" / "tq_client.py"
    spec = importlib.util.spec_from_file_location("tq_client_hold", client_path)
    assert spec and spec.loader
    tq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tq)

    ctrl = tmp_path / "ctrl.json"
    monkeypatch.setenv("TQ_CONTROL_FILE", str(ctrl))
    until = time.time() + 0.35
    ctrl.write_text(json.dumps({"action": "hold", "until": until}), encoding="utf-8")
    slept: list[float] = []

    def _sleep(sec: float) -> None:
        slept.append(sec)
        # Advance fake wall by clearing hold after first sleep.
        ctrl.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(tq.time, "sleep", _sleep)
    assert tq.maybe_hold(sleep_sec=0.2) is True
    assert slept
