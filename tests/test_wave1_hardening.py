import io
import json
import tarfile

import pytest

from tests.helpers.master_worker import seed_queued_task
from taskqueue.safetar import safe_extract
from taskqueue.web.routes_transfer import _upstream_host_allowed


def _tar(n_files=1, size=10):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for i in range(n_files):
            ti = tarfile.TarInfo(f"f{i}.txt")
            ti.size = size
            t.addfile(ti, io.BytesIO(b"x" * size))
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


def test_safe_extract_member_cap(tmp_path):
    with pytest.raises(ValueError, match="too many"):
        safe_extract(_tar(5), tmp_path, max_members=3)


def test_safe_extract_size_cap(tmp_path):
    with pytest.raises(ValueError, match="size"):
        safe_extract(_tar(3, 100), tmp_path, max_total_bytes=200)


def test_safe_extract_ok(tmp_path):
    safe_extract(_tar(2), tmp_path)
    assert (tmp_path / "f1.txt").exists()


def test_upstream_host_blocks_metadata_and_allows_loopback():
    assert not _upstream_host_allowed("169.254.169.254")
    assert not _upstream_host_allowed("0.0.0.0")
    assert not _upstream_host_allowed(None)
    assert _upstream_host_allowed("127.0.0.1")


def test_version_matches_exact_and_compound():
    from taskqueue.scheduler import _version_matches as vm

    assert vm("3.12.1", "==3.12.1")
    assert not vm("3.12.2", "==3.12.1")
    assert vm("3.12.0", ">=3.10,<3.13")
    assert not vm("3.13.0", ">=3.10,<3.13")
    assert vm("3.12", ">=3.12.0")
    assert vm("3.11", "3.10")


def test_fractional_cpu_cores_rounds_up(tmp_path):
    import yaml

    from taskqueue.config import Project
    from taskqueue.db import parse_task_yaml

    y = tmp_path / "t.yaml"
    y.write_text(yaml.dump({"task_id": "a", "command": "x", "resources": {"cpu_cores": 0.5}}), encoding="utf-8")
    t = parse_task_yaml(y, Project(name="p", root=tmp_path, queue_dir=tmp_path))
    assert t.resources.cpu_cores == 1


def test_blocked_projects_ignores_wildcard_lookalike_keys(tmp_path):
    from taskqueue.db import blocked_projects, open_db

    db = open_db(tmp_path / "q.db")
    db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('project_frozenx', '1')")
    db.commit()
    assert blocked_projects(db) == []


def test_service_probe_cache_ignores_gpu_drift_without_vram_requirement(monkeypatch):
    import json

    from taskqueue import scheduler as sch

    calls = []
    monkeypatch.setattr(sch, "_check_services", lambda *a, **k: calls.append(1) or (None, ""))
    sch.clear_service_probe_cache()
    svc = json.dumps([{"name": "s", "health_url": "http://127.0.0.1:1/h"}])
    for used in (1.0, 5.0, 9.0):
        sch.check_services_cached(svc, system_info={"hostname": "h", "gpus": [{"index": 0, "vram_gib": 24, "vram_used_gib": used}]})
    assert len(calls) == 1


def test_shared_gpu_probe_runs_once_per_ttl():
    from taskqueue.resources import shared_gpu_probe

    n = []
    for _ in range(3):
        shared_gpu_probe("t-once", lambda: n.append(1) or 7, ttl=60)
    assert len(n) == 1
    assert shared_gpu_probe("t-once", lambda: 9, ttl=60) == 7


def test_redact_config_masks_notify_url_secrets():
    from taskqueue.web.responses import _redact_config

    out = _redact_config({"auth_token": "t", "notify_url": "https://user:pw@hooks.example.com/services/T0/B0/SECRET?key=abc"})
    assert out["auth_token"] == "***"
    assert out["notify_url"] == "https://hooks.example.com/***"
    assert "SECRET" not in out["notify_url"] and "pw" not in out["notify_url"]
    assert _redact_config({"notify_url": ""})["notify_url"] == ""
    assert _redact_config({"notify_url": "not a url"})["notify_url"] == "***"


def test_requires_paths_migration_failure_is_logged_not_swallowed(tmp_path, capsys, monkeypatch):
    import taskqueue.migrations as mig

    real = mig.json_list

    def boom(v):
        raise RuntimeError("bad json")

    monkeypatch.setattr(mig, "json_list", boom)
    from taskqueue.db import open_db

    conn = open_db(tmp_path / "q.db")
    seed_queued_task(conn, project="p", task_id="t", dispatch="local")
    conn.execute("UPDATE tasks SET requires_paths = ? WHERE task_id = 't'", (json.dumps(["a\\b"]),))
    conn.execute("UPDATE config SET value = '0' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(mig, "json_list", boom)
    conn = open_db(tmp_path / "q.db")
    conn.close()
    assert "requires_paths" in capsys.readouterr().err
    mig.json_list = real


def test_patch_refuses_matrix_variant_ids_but_patches_the_base_task(tmp_path):
    from taskqueue.yaml_patch import patch_task_yaml

    f = tmp_path / "m.yaml"
    f.write_text("task_id: sweep\ncommand: x\nmatrix:\n  seed: [1, 2]\npriority: 5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="matrix"):
        patch_task_yaml(f, {"priority": 9}, task_id="sweep_seed1")
    assert "priority: 5" in f.read_text(encoding="utf-8")
    assert patch_task_yaml(f, {"priority": 9}, task_id="sweep") == ["priority"]
    assert "priority: 9" in f.read_text(encoding="utf-8")


def test_open_db_skips_migration_probes_when_schema_current(tmp_path):
    import sqlite3

    from taskqueue.db import open_db

    open_db(tmp_path / "q.db").close()
    seen: list[str] = []
    real_connect = sqlite3.connect

    def spy(*a, **k):
        c = real_connect(*a, **k)
        c.set_trace_callback(seen.append)
        return c

    sqlite3.connect = spy
    try:
        open_db(tmp_path / "q.db").close()
    finally:
        sqlite3.connect = real_connect
    assert not [s for s in seen if "table_info" in s or "CREATE TABLE" in s.upper()]


def test_finalize_token_matches_is_constant_time_and_rejects_empty():
    from taskqueue.auth import finalize_token_matches

    assert finalize_token_matches("abc", "abc")
    assert not finalize_token_matches("abc", "abd")
    assert not finalize_token_matches(None, "abc")
    assert not finalize_token_matches("", "")
    assert not finalize_token_matches("abc", None)
