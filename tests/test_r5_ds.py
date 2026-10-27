"""Round 5 DS review regressions (F-075–F-078, P-6, P-7)."""

from __future__ import annotations

import inspect
from pathlib import Path

from taskqueue.capacity import build_capacity
from taskqueue.config import Project, validate_config
from taskqueue.yaml_sync import (
    YAML_PARSER_GEN,
    _sync_project,
    _yaml_mtime_cache,
    clear_yaml_mtime_cache,
)
from taskqueue.db import parse_task_yaml_many
from taskqueue.db import get_config, open_db, set_config
from taskqueue.scheduler import check_services_cached, clear_service_probe_cache
from taskqueue.web import make_handler


def _project(tmp_path: Path) -> Project:
    root = tmp_path / "p"
    qdir = root / "queue"
    qdir.mkdir(parents=True)
    return Project(name="p", root=root, queue_dir=qdir)


def test_f075_documented_keys_are_settable_and_survive_restart(tmp_path: Path) -> None:
    assert validate_config("gpu_util_warn_pct", "20") is None
    assert validate_config("ram_settle_sec", "1800") is None
    assert validate_config("ram_leak_warn_gib_per_hour", "0") is None
    assert validate_config("max_concurrent_tasks", "4") is None
    assert validate_config("gpu_util_warn_pct", "101") is not None
    assert validate_config("max_concurrent_tasks", "-1") is not None

    db = open_db(tmp_path / "q.db")
    set_config(db, "max_concurrent_tasks", 4)
    set_config(db, "gpu_util_warn_pct", 20)
    db.close()

    db2 = open_db(tmp_path / "q.db")
    cfg = get_config(db2)
    assert cfg["max_concurrent_tasks"] == 4
    assert cfg["gpu_util_warn_pct"] == 20
    assert cfg["ram_settle_sec"] == 1800
    assert cfg["ram_leak_warn_gib_per_hour"] == 1.0
    db2.close()


def test_f076_oversized_yaml_does_not_delete_existing_task(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    path = proj.queue_dir / "big.yaml"
    path.write_text(
        "task_id: big\ncommand: echo\nargs: [ok]\npriority: 50\nenabled: true\n",
        encoding="utf-8",
    )
    db = open_db(tmp_path / "q.db")
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    assert db.execute(
        "SELECT task_id FROM tasks WHERE project='p' AND task_id='big'"
    ).fetchone() is not None

    path.write_bytes(b"x" * (512 * 1024 + 1))
    _sync_project(db, proj)
    row = db.execute(
        "SELECT task_id, status FROM tasks WHERE project='p' AND task_id='big'"
    ).fetchone()
    assert row is not None
    db.close()


def test_f077_parser_gen_busts_mtime_cache(tmp_path: Path, monkeypatch) -> None:
    proj = _project(tmp_path)
    path = proj.queue_dir / "a.yaml"
    path.write_text(
        "task_id: a\ncommand: echo\nargs: []\npriority: 50\n",
        encoding="utf-8",
    )
    db = open_db(tmp_path / "q.db")
    clear_yaml_mtime_cache()
    _sync_project(db, proj)
    cached = _yaml_mtime_cache[str(path)]
    assert cached[1] == YAML_PARSER_GEN

    calls = {"n": 0}
    real = parse_task_yaml_many

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr("taskqueue.yaml_sync.parse_task_yaml_many", _spy)
    _sync_project(db, proj)
    assert calls["n"] == 0

    monkeypatch.setattr("taskqueue.yaml_sync.YAML_PARSER_GEN", YAML_PARSER_GEN + 1)
    _sync_project(db, proj)
    assert calls["n"] == 1
    assert _yaml_mtime_cache[str(path)][1] == YAML_PARSER_GEN + 1
    db.close()


def test_f078_service_probe_cached_for_ttl(monkeypatch) -> None:
    calls = {"n": 0}

    def _fake(services_json, *, allow_remote=False, system_info=None):
        calls["n"] += 1
        return ("down", "tq.block.service_down")

    monkeypatch.setattr("taskqueue.scheduler._check_services", _fake)
    clear_service_probe_cache()
    assert check_services_cached('[{"health_url":"http://127.0.0.1:9"}]')[0] == "down"
    assert check_services_cached('[{"health_url":"http://127.0.0.1:9"}]')[0] == "down"
    assert calls["n"] == 1
    clear_service_probe_cache()


def test_capacity_skips_detect_system_when_snapshot_given(tmp_path: Path, monkeypatch) -> None:
    def _boom():
        raise AssertionError("detect_system should not run")

    monkeypatch.setattr("taskqueue.capacity.detect_system", _boom)
    db = open_db(tmp_path / "q.db")
    cap = build_capacity(
        db,
        {"cpu_limit_pct": 50, "gpu_limit_pct": 100, "ram_limit_pct": 80},
        {"cpu_total_cores": 4, "ram_total_gib": 8, "gpu_vram_total_gib": 0, "gpus": []},
    )
    assert cap["master"]["cpu_cores_total"] == 4
    assert cap["master"]["ram_gib_total"] == 8
    db.close()


def test_rconn_opens_without_migrate() -> None:
    from taskqueue.web.handler import Handler
    import threading

    import taskqueue.db as dbmod

    seen = {}

    def fake_open(path, **kw):
        seen.update(kw)
        return object()

    class H(Handler):
        _tls = threading.local()
        _db_path = "x.db"

    orig = dbmod.open_db
    dbmod.open_db = fake_open
    try:
        H.__new__(H)._rconn()
    finally:
        dbmod.open_db = orig
    assert seen.get("migrate") is False
