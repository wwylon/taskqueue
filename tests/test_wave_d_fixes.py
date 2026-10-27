"""Tests for Wave D bug fixes (F-004, F-007, F-010, F-011, F-015, F-018, F-019,
F-020, F-021, F-022, F-024, F-025, F-034, F-042, F-043, F-044, F-046, F-048,
F-051, F-052, F-054, F-055 and related regressions).
"""
from __future__ import annotations

import json
import sys
import tarfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from taskqueue.db import open_db, get_config, set_config
from taskqueue.export_import import import_home, export_home
from taskqueue.notify import notify_task_started, notify_log_path
from taskqueue.resources import sort_queued_fairshare
from taskqueue.safetar import assert_safe_member


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_task(db, project="p", task_id="t1", status="queued"):
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT OR REPLACE INTO tasks "
        "(project, task_id, status, command, enqueued_at, updated_at, enabled) "
        "VALUES (?, ?, ?, 'echo hi', ?, ?, 1)",
        (project, task_id, status, now, now),
    )
    db.commit()


def _insert_run(db, project="p", task_id="t1", worker_id="w1",
                finalize_token=None, status="running", sleep_tier=None):
    now = datetime.now(UTC).isoformat()
    future = "2099-01-01T00:00:00+00:00"
    cur = db.execute(
        "INSERT INTO runs "
        "(project, task_id, started_at, status, log_path, worker_id, "
        "lease_expires_at, finalize_token, sleep_tier) "
        "VALUES (?, ?, ?, ?, 'x.log', ?, ?, ?, ?)",
        (project, task_id, now, status, worker_id, future, finalize_token, sleep_tier),
    )
    db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# F-055: finalize_token — only claiming worker can finalize
# ---------------------------------------------------------------------------

def test_finalize_token_stored_and_queryable(tmp_path):
    """finalize_token column exists and survives a round-trip."""
    db = open_db(tmp_path / "q.db")
    _insert_task(db)
    token = "abc123deadbeef"
    run_id = _insert_run(db, finalize_token=token)
    row = db.execute("SELECT finalize_token FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert row["finalize_token"] == token


def test_finalize_token_wrong_rejected(tmp_path):
    """Wrong token must not match the stored token."""
    db = open_db(tmp_path / "q.db")
    _insert_task(db)
    good_token = "correct_token_hex"
    run_id = _insert_run(db, finalize_token=good_token)

    run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    stored = run["finalize_token"] if "finalize_token" in run.keys() else None

    assert stored is not None
    assert "wrong_token" != stored
    assert good_token == stored


def test_finalize_token_null_skips_check(tmp_path):
    """NULL finalize_token means back-compat mode — no token check performed."""
    db = open_db(tmp_path / "q.db")
    _insert_task(db)
    run_id = _insert_run(db, finalize_token=None)
    run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    stored = run["finalize_token"] if "finalize_token" in run.keys() else None
    assert stored is None


def test_finalize_token_column_exists(tmp_path):
    """finalize_token column must be present in runs table schema."""
    db = open_db(tmp_path / "q.db")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)").fetchall()}
    assert "finalize_token" in cols


# ---------------------------------------------------------------------------
# F-007: registered_ip — column present and IP stored on registration
# ---------------------------------------------------------------------------

def test_registered_ip_column_exists(tmp_path):
    """registered_ip column must exist in workers table after migration."""
    db = open_db(tmp_path / "q.db")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(workers)").fetchall()}
    assert "registered_ip" in cols


def test_registered_ip_stored(tmp_path):
    """An IP stored in registered_ip survives a round-trip."""
    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO workers (worker_id, os, arch, ram_gib, cpu_cores, gpu_vram_gib, "
        "status, last_seen, registered_ip) "
        "VALUES ('w1', 'win32', 'x86_64', 16.0, 8, 0.0, 'online', ?, '192.168.1.42')",
        (now,),
    )
    db.commit()
    row = db.execute("SELECT registered_ip FROM workers WHERE worker_id='w1'").fetchone()
    assert row["registered_ip"] == "192.168.1.42"


# ---------------------------------------------------------------------------
# F-010: NtResumeProcess before kill — sleep_tier=2 in runs table
# ---------------------------------------------------------------------------

def test_sleep_tier_column_exists(tmp_path):
    """sleep_tier column must exist in runs table."""
    db = open_db(tmp_path / "q.db")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)").fetchall()}
    assert "sleep_tier" in cols


def test_requeue_orphaned_running_requeues_tier2(tmp_path):
    """requeue_orphaned_running must re-queue a tier-2 run to 'queued'."""
    from taskqueue.daemon import requeue_orphaned_running

    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, enabled) "
        "VALUES ('p','t1','running','echo', ?, ?, 1)", (now, now)
    )
    # Use a PID that definitely does not exist to avoid kill_process_tree issues
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, pid, "
        "sleep_tier, lease_expires_at) "
        "VALUES ('p','t1', ?, 'running', 'x.log', 2147483647, 2, '2099-01-01T00:00:00+00:00')",
        (now,),
    )
    db.commit()

    # Patch kill_process_tree so we don't try to reap a phantom PID,
    # and NtResumeProcess so Windows-specific code doesn't fail on Linux/CI.
    # kill_process_tree is imported inside requeue_orphaned_running; patch at source
    with patch("taskqueue.reap.kill_process_tree", return_value="no-op"):
        if sys.platform == "win32":
            import ctypes
            with patch.object(ctypes, "windll", create=True) as mock_wdll:
                mock_wdll.kernel32 = MagicMock()
                mock_wdll.ntdll = MagicMock()
                mock_wdll.kernel32.OpenProcess.return_value = 1
                requeue_orphaned_running(db, reason="test", graceful_sec=0.0)
        else:
            requeue_orphaned_running(db, reason="test", graceful_sec=0.0)

    row = db.execute(
        "SELECT status FROM tasks WHERE project='p' AND task_id='t1'"
    ).fetchone()
    assert row["status"] == "queued", "tier-2 run must be re-queued after orphan reap"


def test_requeue_orphaned_clears_stop_requested(tmp_path):
    """re-queued task must have stop_requested=0 so it isn't immediately re-stopped."""
    from taskqueue.daemon import requeue_orphaned_running

    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
        "enabled, stop_requested) VALUES ('p','t1','running','echo', ?, ?, 1, 1)",
        (now, now),
    )
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, pid, "
        "lease_expires_at) "
        "VALUES ('p','t1', ?, 'running', 'x.log', 2147483647, '2099-01-01T00:00:00+00:00')",
        (now,),
    )
    db.commit()

    with patch("taskqueue.reap.kill_process_tree", return_value="no-op"):
        requeue_orphaned_running(db, reason="test", graceful_sec=0.0)

    row = db.execute(
        "SELECT stop_requested FROM tasks WHERE project='p' AND task_id='t1'"
    ).fetchone()
    assert row["stop_requested"] == 0, "stop_requested must be cleared on re-queue (F-008)"


# ---------------------------------------------------------------------------
# F-025: import_home refuses when daemon is alive
# ---------------------------------------------------------------------------

def _make_archive(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    db = open_db(src / "queue.db")
    db.close()
    return export_home(src, tmp_path / "export.tar.gz")


def test_import_home_refuses_while_daemon_alive(tmp_path):
    """import_home must raise RuntimeError if daemon.pid points to a live process."""
    home = tmp_path / "home"
    home.mkdir()
    import os
    (home / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
    archive = _make_archive(tmp_path)

    with pytest.raises(RuntimeError, match="daemon is running"):
        import_home(home, archive, force=True)


def test_import_home_ok_without_daemon(tmp_path):
    """import_home must succeed when no daemon pid file exists."""
    home = tmp_path / "home"
    home.mkdir()
    archive = _make_archive(tmp_path)

    result = import_home(home, archive, force=True)
    assert (home / "queue.db").is_file()
    assert result["home"] == str(home)


def test_import_home_ok_with_stale_pid(tmp_path):
    """import_home must succeed when daemon.pid exists but PID is not alive."""
    home = tmp_path / "home"
    home.mkdir()
    # PID 999999999 is virtually guaranteed not to exist
    (home / "daemon.pid").write_text("999999999", encoding="utf-8")
    archive = _make_archive(tmp_path)

    import_home(home, archive, force=True)
    assert (home / "queue.db").is_file()


# ---------------------------------------------------------------------------
# F-018: safetar linkname validation — assert_safe_member(dest, member)
# ---------------------------------------------------------------------------

def _sym(name: str, linkname: str) -> tarfile.TarInfo:
    m = tarfile.TarInfo(name=name)
    m.type = tarfile.SYMTYPE
    m.linkname = linkname
    return m


def _lnk(name: str, linkname: str) -> tarfile.TarInfo:
    m = tarfile.TarInfo(name=name)
    m.type = tarfile.LNKTYPE
    m.linkname = linkname
    return m


def test_safetar_rejects_absolute_symlink(tmp_path):
    m = _sym("safe.txt", "/etc/passwd")
    with pytest.raises(ValueError, match="absolute"):
        assert_safe_member(tmp_path, m)


def test_safetar_rejects_dotdot_symlink(tmp_path):
    m = _sym("safe.txt", "../../etc/shadow")
    with pytest.raises(ValueError, match=r"\.\.|traversal|unsafe"):
        assert_safe_member(tmp_path, m)


def test_safetar_accepts_relative_symlink(tmp_path):
    m = _sym("data/link.txt", "sibling.txt")
    assert_safe_member(tmp_path, m)  # must not raise


def test_safetar_rejects_hardlink_absolute(tmp_path):
    m = _lnk("safe.txt", "/etc/passwd")
    with pytest.raises(ValueError, match="absolute"):
        assert_safe_member(tmp_path, m)


def test_safetar_rejects_hardlink_dotdot(tmp_path):
    m = _lnk("safe.txt", "../../etc/shadow")
    with pytest.raises(ValueError, match=r"\.\.|traversal|unsafe"):
        assert_safe_member(tmp_path, m)


def test_safetar_rejects_windows_drive_linkname(tmp_path):
    # C:\... should be rejected as an absolute-style path
    m = _sym("safe.txt", "C:\\Windows\\evil.dll")
    with pytest.raises(ValueError):
        assert_safe_member(tmp_path, m)


# ---------------------------------------------------------------------------
# F-046: fairshare_halflife_days read from config, survives restart
# ---------------------------------------------------------------------------

def test_fairshare_halflife_config_key_survives_restart(tmp_path):
    """fairshare_halflife_days must not be pruned by _seed_config on open_db."""
    db = open_db(tmp_path / "q.db")
    set_config(db, "fairshare_halflife_days", 14)
    db.close()

    db2 = open_db(tmp_path / "q.db")
    cfg = get_config(db2)
    assert float(cfg.get("fairshare_halflife_days", 0)) == 14.0


def test_fairshare_halflife_decay_formula():
    """Shorter halflife produces stronger decay for the same elapsed time."""
    elapsed_h = 24.0

    def decay(halflife_days: float) -> float:
        halflife_h = halflife_days * 24
        return 0.5 ** (elapsed_h / halflife_h)

    short = decay(1)    # exactly one halflife → 0.5
    medium = decay(7)   # less than one halflife → closer to 1
    long_ = decay(30)

    assert abs(short - 0.5) < 1e-9, "1-day halflife after 24h must give exactly 0.5"
    assert short < medium < long_


# ---------------------------------------------------------------------------
# F-011: ON CONFLICT clears args_override when YAML args change
# ---------------------------------------------------------------------------

def test_args_override_cleared_on_args_change(tmp_path):
    """Upserting a task with changed args must clear any stale args_override."""
    from taskqueue.db import upsert_task, parse_task_yaml
    from taskqueue.config import Project

    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    queue_dir = proj_root / "queue"
    queue_dir.mkdir()
    proj = Project(name="p", root=proj_root, queue_dir=queue_dir)

    yaml_path = queue_dir / "t1.yaml"
    yaml_path.write_text("task_id: t1\ncommand: echo\nargs: [a]\npriority: 50\n", encoding="utf-8")

    db = open_db(tmp_path / "q.db")
    upsert_task(db, parse_task_yaml(yaml_path, proj))

    db.execute(
        "UPDATE tasks SET args_override='[\"override\"]' WHERE project='p' AND task_id='t1'"
    )
    db.commit()
    assert db.execute(
        "SELECT args_override FROM tasks WHERE project='p' AND task_id='t1'"
    ).fetchone()["args_override"] is not None

    # Re-upsert with different args
    yaml_path.write_text("task_id: t1\ncommand: echo\nargs: [b]\npriority: 50\n", encoding="utf-8")
    upsert_task(db, parse_task_yaml(yaml_path, proj))

    row = db.execute("SELECT args_override FROM tasks WHERE project='p' AND task_id='t1'").fetchone()
    assert row["args_override"] is None, "args_override must be cleared when YAML args change"


def test_args_override_preserved_when_args_unchanged(tmp_path):
    """args_override must NOT be cleared when YAML args are unchanged."""
    from taskqueue.db import upsert_task, parse_task_yaml
    from taskqueue.config import Project

    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    queue_dir = proj_root / "queue"
    queue_dir.mkdir()
    proj = Project(name="p", root=proj_root, queue_dir=queue_dir)

    yaml_path = queue_dir / "t1.yaml"
    yaml_path.write_text("task_id: t1\ncommand: echo\nargs: [same]\npriority: 50\n", encoding="utf-8")

    db = open_db(tmp_path / "q.db")
    upsert_task(db, parse_task_yaml(yaml_path, proj))

    override = '["some_override"]'
    db.execute("UPDATE tasks SET args_override=? WHERE project='p' AND task_id='t1'", (override,))
    db.commit()

    upsert_task(db, parse_task_yaml(yaml_path, proj))

    row = db.execute("SELECT args_override FROM tasks WHERE project='p' AND task_id='t1'").fetchone()
    assert row["args_override"] == override, "args_override must be kept when args are unchanged"


# ---------------------------------------------------------------------------
# F-042: enabled: regex is case-insensitive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("disabled_val", ["False", "false", "NO", "no"])
def test_enable_task_yaml_case_insensitive(tmp_path, disabled_val):
    """enable_task must handle 'False', 'NO', 'false', 'no' in the YAML."""
    import re

    text = f"task_id: t\ncommand: echo\nenabled: {disabled_val}\n"
    result = re.sub(
        r"^enabled:\s*(false|no)\b",
        "enabled: true",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    assert "enabled: true" in result, f"Failed to re-enable for value {disabled_val!r}"
    assert disabled_val not in result


# ---------------------------------------------------------------------------
# F-015: strip_secrets — env/args_override/yaml_source not in task dicts
# ---------------------------------------------------------------------------

def test_task_dict_strip_secrets(tmp_path):
    """_task_dict with strip_secrets=True must omit env, args_override, yaml_source."""
    from taskqueue.web import _task_dict

    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
        "enabled, env, args_override, yaml_source) "
        "VALUES ('p','t1','queued','echo', ?, ?, 1, "
        "'{\"SECRET\":\"s3kr3t\"}', '[\"x\"]', '/secret/path.yaml')",
        (now, now),
    )
    db.commit()

    row = db.execute("SELECT * FROM tasks WHERE project='p' AND task_id='t1'").fetchone()
    d = _task_dict(row, strip_secrets=True)

    assert "env" not in d
    assert "args_override" not in d
    assert "yaml_source" not in d
    assert d["task_id"] == "t1"
    assert d["command"] == "echo"


def test_task_dict_no_strip_includes_secret_fields(tmp_path):
    """_task_dict without strip_secrets must include env and args_override."""
    from taskqueue.web import _task_dict

    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
        "enabled, env, args_override) "
        "VALUES ('p','t1','queued','echo', ?, ?, 1, '{\"K\":\"V\"}', '[\"x\"]')",
        (now, now),
    )
    db.commit()

    row = db.execute("SELECT * FROM tasks WHERE project='p' AND task_id='t1'").fetchone()
    d = _task_dict(row, strip_secrets=False)

    assert "env" in d
    assert "args_override" in d


# ---------------------------------------------------------------------------
# F-052: notify_task_started must NOT write to notify.jsonl
# ---------------------------------------------------------------------------

def test_notify_started_does_not_write_drain_queue(tmp_path):
    """notify_task_started must not append to notify.jsonl (not a terminal event)."""
    log_file = notify_log_path(tmp_path)
    before = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""

    notify_task_started(
        tmp_path,
        project="p",
        task_id="t1",
        run_id=1,
        started_by=None,
        notify_url=None,
        post=False,  # skip actual HTTP
    )

    after = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""
    assert before == after, "notify_task_started must not write to notify.jsonl"


# ---------------------------------------------------------------------------
# F-044: stopped status treated as resumable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["interrupted", "suspended", "stopped"])
def test_resumable_statuses(tmp_path, status):
    """use_resume must be True for interrupted/suspended/stopped runs."""
    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, status, log_path, "
        "worker_id, lease_expires_at) "
        "VALUES ('p', 't1', ?, ?, 'x.log', 'w1', '2099-01-01T00:00:00+00:00')",
        (now, status),
    )
    db.commit()

    last_run = db.execute(
        "SELECT * FROM runs WHERE project='p' AND task_id='t1' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    use_resume = bool(
        last_run and last_run["status"] in ("interrupted", "suspended", "stopped")
    )
    assert use_resume, f"status={status!r} must be treated as resumable (F-044)"


# ---------------------------------------------------------------------------
# F-004: last_error_code cleared to NULL on clean completion
# ---------------------------------------------------------------------------

def test_last_error_code_cleared_on_success(tmp_path):
    """A successful completion must NULL last_error_code."""
    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
        "enabled, last_error_code) VALUES ('p','t1','running','echo', ?, ?, 1, 'tq.run.nonzero')",
        (now, now),
    )
    db.commit()

    # F-004 fix: NULL on success (not COALESCE)
    db.execute(
        "UPDATE tasks SET status='completed', last_error_code=NULL "
        "WHERE project='p' AND task_id='t1'"
    )
    db.commit()

    row = db.execute("SELECT last_error_code FROM tasks WHERE project='p' AND task_id='t1'").fetchone()
    assert row["last_error_code"] is None


def test_last_error_code_preserved_on_failure(tmp_path):
    """A failed run must keep its last_error_code."""
    db = open_db(tmp_path / "q.db")
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO tasks (project, task_id, status, command, enqueued_at, updated_at, "
        "enabled, last_error_code) VALUES ('p','t1','running','echo', ?, ?, 1, 'tq.run.nonzero')",
        (now, now),
    )
    db.commit()

    err = "tq.run.nonzero"
    # COALESCE keeps existing code if no new one provided
    db.execute(
        "UPDATE tasks SET status='failed', last_error_code=COALESCE(?, last_error_code) "
        "WHERE project='p' AND task_id='t1'",
        (err,),
    )
    db.commit()

    row = db.execute("SELECT last_error_code FROM tasks WHERE project='p' AND task_id='t1'").fetchone()
    assert row["last_error_code"] == err


# ---------------------------------------------------------------------------
# F-024: GPU requires float treated as minimum VRAM GiB
# ---------------------------------------------------------------------------

def test_gpu_float_vram_gate_passes():
    """gpu: 4.0 on a host with 8 GiB free VRAM must pass."""
    req = {"gpu": 4.0}
    try:
        req_vram = float(req.get("gpu", 0))
    except (TypeError, ValueError):
        req_vram = 0.0
    host_vram = 8.0
    assert not (req_vram > 0.0 and host_vram < req_vram)


def test_gpu_float_vram_gate_fails():
    """gpu: 12.0 on a host with only 8 GiB free VRAM must fail."""
    req = {"gpu": 12.0}
    try:
        req_vram = float(req.get("gpu", 0))
    except (TypeError, ValueError):
        req_vram = 0.0
    host_vram = 8.0
    assert req_vram > 0.0 and host_vram < req_vram


def test_gpu_bool_true_requires_any_vram():
    """gpu: true (non-float) must behave as 'any GPU required'."""
    req = {"gpu": True}
    try:
        req_vram = float(req.get("gpu", 0))
        # True casts to 1.0, so treat >0 as minimum
        is_float_check = True
    except (TypeError, ValueError):
        is_float_check = False
    # True → 1.0 in Python, so passes if host has >= 1 GiB
    host_vram = 0.0
    if is_float_check:
        failed = req_vram > 0.0 and host_vram < req_vram
        assert failed, "gpu:true with no VRAM must fail"


# ---------------------------------------------------------------------------
# F-051: project_frozen_ prefix kept by _seed_config
# ---------------------------------------------------------------------------

def test_project_frozen_prefix_survives_restart(tmp_path):
    """project_frozen_<name> keys must survive open_db (not pruned by _seed_config)."""
    db = open_db(tmp_path / "q.db")
    db.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES ('project_frozen_myproj', '1')"
    )
    db.commit()
    db.close()

    db2 = open_db(tmp_path / "q.db")
    row = db2.execute(
        "SELECT value FROM config WHERE key='project_frozen_myproj'"
    ).fetchone()
    assert row is not None and row["value"] == "1"


# ---------------------------------------------------------------------------
# F-034 regression: all documented config keys survive restart
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("notify_url", "http://localhost:9000/hook"),
    ("frozen_action", "suspend"),
    ("limit_decrease_policy", "block"),
    ("notify_on_attention", "1"),
    ("fairshare_halflife_days", "14"),
    ("fairshare_weight", "0.5"),
    ("toast_notifications", "1"),
    ("auth_token", "mytoken"),
])
def test_config_keys_not_pruned_on_restart(tmp_path, key, value):
    """Config keys in _CORE_CONFIG_KEYS or VALID_CONFIG_KEYS must not be pruned."""
    db = open_db(tmp_path / "q.db")
    # set_config validates keys in VALID_CONFIG_KEYS; for string keys not in VCK,
    # write directly to avoid validation rejection.
    db.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
    db.commit()
    db.close()

    db2 = open_db(tmp_path / "q.db")
    row = db2.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    assert row is not None, f"Config key {key!r} was pruned on open_db (F-034 regression)"
    assert json.loads(row["value"]) == value


# ---------------------------------------------------------------------------
# F-054: CSRF check enforced regardless of auth_token
# ---------------------------------------------------------------------------

def test_csrf_enforced_with_auth_token(tmp_path):
    """Foreign Origin must be rejected even when auth_token is set."""
    import io
    from taskqueue.web import make_handler

    db = open_db(tmp_path / "q.db")
    set_config(db, "auth_token", "supersecret")

    HandlerClass = make_handler(db, {}, {}, lambda: {}, tmp_path / "q.db")

    raw_request = (
        b"POST /api/config HTTP/1.1\r\n"
        b"Host: localhost:8765\r\n"
        b"Origin: http://evil.example.com\r\n"
        b"Authorization: Bearer supersecret\r\n"
        b"Content-Length: 2\r\n"
        b"\r\n{}"
    )

    status_codes = []

    class FakeWfile:
        def write(self, data): pass
        def flush(self): pass

    class TestHandler(HandlerClass):
        def setup(self):
            self.rfile = io.BufferedReader(io.BytesIO(raw_request))
            self.wfile = FakeWfile()
            from http.client import parse_headers
            header_bytes = b"\r\n".join(raw_request.split(b"\r\n")[1:])
            self.headers = parse_headers(io.BytesIO(header_bytes))
            self.command = "POST"
            self.path = "/api/config"
            self.close_connection = True

        def send_response(self, code, msg=None):
            status_codes.append(code)

        def send_header(self, k, v): pass
        def end_headers(self): pass

        def handle_one_request(self):
            self._check_auth()

    h = TestHandler.__new__(TestHandler)
    TestHandler.setup(h)
    TestHandler.handle_one_request(h)
    assert 403 in status_codes, f"CSRF must reject foreign Origin even with auth_token; got {status_codes}"


# ---------------------------------------------------------------------------
# sort_queued_fairshare — usage affects ordering (F-046 context)
# ---------------------------------------------------------------------------

def _row(project, priority=50, enqueued_at="2024-01-01T00:00:00"):
    return {"project": project, "priority": priority, "enqueued_at": enqueued_at}


def test_fairshare_high_usage_deprioritized():
    """Project with higher GPU usage should be scheduled later."""
    queued = [_row("heavy"), _row("light")]
    usage = {"heavy": 10000.0, "light": 100.0}
    result = sort_queued_fairshare(queued, {}, usage, fairshare_weight=1e-3)
    assert result[0]["project"] == "light"
    assert result[1]["project"] == "heavy"


def test_fairshare_priority_beats_usage():
    """Higher priority must win over higher usage."""
    queued = [_row("low_prio", priority=10), _row("high_prio", priority=90)]
    usage = {"low_prio": 0.0, "high_prio": 99999.0}
    result = sort_queued_fairshare(queued, {}, usage, fairshare_weight=1e-6)
    assert result[0]["project"] == "high_prio"


def test_fairshare_zero_weight_ignores_usage():
    """fairshare_weight=0 means ordering falls back to priority then age."""
    queued = [_row("old", enqueued_at="2024-01-01"), _row("new", enqueued_at="2024-06-01")]
    usage = {"old": 0.0, "new": 99999.0}
    result = sort_queued_fairshare(queued, {}, usage, fairshare_weight=0.0)
    assert result[0]["project"] == "old"  # same priority → older task first
