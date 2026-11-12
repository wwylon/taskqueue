"""Pytest fixtures for master–worker tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.master_worker import make_handler_env


@pytest.fixture
def mw_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TQDAEMON_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def mw_handler(mw_home: Path):
    HandlerClass, db, home = make_handler_env(mw_home)
    yield HandlerClass, db, home
    db.close()


@pytest.fixture(autouse=True)
def _reset_master_instance_id():
    """run_daemon() sets a process-global holder id; keep it from leaking across tests."""
    from tqdaemon import db as _db
    from tqdaemon import lease as _lease

    _db.set_master_instance_id(None)
    _lease._yield_until = 0.0  # /api/yield-master back-off is process-global too
    yield
    _db.set_master_instance_id(None)
    _lease._yield_until = 0.0


def pytest_collection_modifyitems(config, items):
    """Auto-mark the node-driven web UI tests so `pytest -m "not js"` is a fast Python-only loop."""
    for item in items:
        if item.fspath.basename.startswith("test_js_"):
            item.add_marker(pytest.mark.js)
