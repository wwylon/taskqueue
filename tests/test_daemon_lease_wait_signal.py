"""SIGTERM while waiting for the master lease must exit run_daemon cleanly."""
from __future__ import annotations

import signal
import threading
from pathlib import Path

from taskqueue import daemon


def test_sigterm_during_lease_wait_returns(tmp_path: Path, monkeypatch) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    calls = {"n": 0}

    def fake_cas(conn, instance):
        calls["n"] += 1
        if calls["n"] == 1:
            signal.raise_signal(signal.SIGTERM)
        return False

    monkeypatch.setattr(daemon, "_cas_master_lease", fake_cas)
    t = threading.Timer(60, lambda: signal.raise_signal(signal.SIGTERM))
    t.daemon = True
    t.start()
    try:
        daemon.run_daemon(tmp_path)
    finally:
        t.cancel()
        for s, h in saved.items():
            signal.signal(s, h)
    assert calls["n"] == 1
