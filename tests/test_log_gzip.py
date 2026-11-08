"""Wave 7: retention_log_gzip_days — gzip finished logs; every reader decompresses transparently."""

from __future__ import annotations

import gzip
import os
import time
from pathlib import Path

from taskqueue._cli_prune import compress_finished_logs, prune_logs
from taskqueue.logio import compress_log, gz_path, read_log_tail, readable_log, tail_log_lines
from tests.helpers.master_worker import http_request, make_handler_env

TEXT = "".join(f"line {i} epoch loss {i / 7:.4f}\n" for i in range(2000))


def _log(dir_: Path, name: str, age_days: float, text: str = TEXT) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / name
    p.write_bytes(text.encode("utf-8"))  # exact bytes: write_text would add CRs on Windows
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def test_compress_keeps_mtime_and_readers_fall_back_to_the_gz(tmp_path: Path) -> None:
    p = _log(tmp_path, "a.log", 40)
    mtime = p.stat().st_mtime
    saved = compress_log(p)
    gz = gz_path(p)
    assert not p.exists() and gz.exists() and saved > 0
    assert abs(gz.stat().st_mtime - mtime) < 1  # age-based deletion is not reset
    assert gzip.decompress(gz.read_bytes()).decode() == TEXT

    plain = readable_log(p)
    assert plain != p and plain.read_bytes() == TEXT.encode("utf-8")
    assert readable_log(p) == plain  # cached copy reused
    assert read_log_tail(p, max_bytes=64).text.endswith("line 1999 epoch loss 285.5714\n")
    assert tail_log_lines(p, 2)[-1] == "line 1999 epoch loss 285.5714"
    missing = tmp_path / "never.log"
    assert readable_log(missing) == missing  # callers keep their "log missing" handling


def test_retention_compresses_old_finished_logs_only(tmp_path: Path) -> None:
    logs = tmp_path / "logs" / "p"
    old, recent, running, empty = (
        _log(logs, "old.log", 40),
        _log(logs, "recent.log", 1),
        _log(logs, "running.log", 40),
        _log(logs, "empty.log", 40, ""),
    )
    cfg = {"retention_log_gzip_days": 7}
    unfinished = {str(running.resolve())}
    dry = compress_finished_logs(tmp_path, cfg, dry_run=True, unfinished=unfinished)
    assert dry["logs_compressed"] == 1 and old.exists()  # dry run changes nothing
    res = compress_finished_logs(tmp_path, cfg, dry_run=False, unfinished=unfinished)
    assert res["logs_compressed"] == 1 and res["logs_bytes_compressed"] > 0
    assert not old.exists() and gz_path(old).exists()
    assert recent.exists() and running.exists() and empty.exists()
    assert compress_finished_logs(tmp_path, {}, dry_run=False)["logs_compressed"] == 0  # off by default


def test_age_prune_deletes_old_gz_but_keeps_a_protected_one(tmp_path: Path) -> None:
    logs = tmp_path / "logs" / "p"
    a, b = _log(logs, "a.log", 200), _log(logs, "b.log", 200)
    compress_log(a)
    compress_log(b)
    res = prune_logs(tmp_path, {"retention_log_days": 90}, dry_run=False, protected={str(b.resolve())})
    assert res["logs_deleted"] == 1
    assert not gz_path(a).exists() and gz_path(b).exists()


def test_api_logs_serves_a_gzipped_log(tmp_path: Path) -> None:
    H, db, _ = make_handler_env(tmp_path)
    log = _log(tmp_path / "logs" / "p", "t.log", 40)
    db.execute(
        "INSERT INTO runs (project, task_id, started_at, finished_at, status, exit_code, log_path) "
        "VALUES ('p', 't', '2026-01-01T00:00:00Z', '2026-01-01T00:10:00Z', 'completed', 0, ?)",
        (str(log),),
    )
    db.commit()
    run_id = db.execute("SELECT run_id FROM runs").fetchone()[0]
    compress_log(log)
    code, body = http_request(H, "GET", f"/api/logs/{run_id}")
    text = body.decode() if isinstance(body, bytes) else str(body)
    assert code == 200 and "line 1999 epoch loss" in text
    db.close()
