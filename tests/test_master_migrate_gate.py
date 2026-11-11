"""In-process gate for ``tq master-migrate`` before I3 two-machine dogfood.

Asserts the CLI refuses a non-standby target (leader_epoch / redirect remain
hardware-blocked; this locks the first check in the migrate flow).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

from tqdaemon.db import open_db


def test_master_migrate_rejects_non_standby(tmp_path: Path, monkeypatch):
    from tqdaemon._cli_admin import cmd_master_migrate

    home = tmp_path / "home"
    home.mkdir()
    open_db(home / "queue.db").close()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"mode": "master", "ok": True}).encode()

    def _urlopen(req, timeout=10):
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    rc = cmd_master_migrate(home, "http://127.0.0.1:9999")
    assert rc == 1


def test_master_migrate_unreachable_target(tmp_path: Path, monkeypatch):
    from tqdaemon._cli_admin import cmd_master_migrate

    home = tmp_path / "home"
    home.mkdir()
    open_db(home / "queue.db").close()

    def _urlopen(req, timeout=10):
        raise URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    rc = cmd_master_migrate(home, "http://127.0.0.1:9")
    assert rc == 1
