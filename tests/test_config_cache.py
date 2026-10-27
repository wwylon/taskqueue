"""get_config cache: keyed on connection identity and expiring, so out-of-band writes surface."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import taskqueue.db as dbmod
from taskqueue.db import clear_config_cache, get_config, open_db, set_config


def test_other_connection_write_visible_after_ttl(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "q.db"
    reader = open_db(path)
    writer = sqlite3.connect(str(path))
    clear_config_cache()
    assert get_config(reader).get("usage_tracking", 1) == 1

    # Out-of-band write (e.g. another process running `tq config set`): no invalidation.
    writer.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('usage_tracking', '0')")
    writer.commit()

    # Within the TTL the cached value is served; once it expires the change is seen.
    assert get_config(reader).get("usage_tracking", 1) == 1
    monkeypatch.setattr(dbmod, "_CONFIG_CACHE_TTL_SEC", 0.0)
    assert get_config(reader).get("usage_tracking", 1) == 0
    writer.close()
    reader.close()


def test_cache_is_not_shared_across_connections(tmp_path: Path) -> None:
    a = open_db(tmp_path / "a.db")
    b = open_db(tmp_path / "b.db")
    clear_config_cache()
    set_config(a, "usage_tracking", 0)
    # Connection b must not inherit a's cached view, even if a's object was recycled.
    assert get_config(b).get("usage_tracking", 1) == 1
    a.close()
    b.close()
