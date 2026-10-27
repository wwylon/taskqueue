"""Pytest fixtures for master–worker tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.master_worker import make_handler_env, seed_master_lease


@pytest.fixture
def mw_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TASKQUEUE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def mw_handler(mw_home: Path):
    HandlerClass, db, home = make_handler_env(mw_home)
    yield HandlerClass, db, home
    db.close()


@pytest.fixture(autouse=True)
def _reset_master_instance_id():
    """run_daemon() sets a process-global holder id; keep it from leaking across tests."""
    from taskqueue import db as _db

    _db.set_master_instance_id(None)
    yield
    _db.set_master_instance_id(None)
