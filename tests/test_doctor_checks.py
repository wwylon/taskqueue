"""tq doctor checks: levels, JSON output and exit code."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from taskqueue import cli
from taskqueue.doctor_checks import run_checks


def test_fresh_home_warns_but_passes(tmp_path: Path) -> None:
    r = run_checks(tmp_path)
    by = {c["name"]: c["level"] for c in r["checks"]}
    assert r["ok"] is True and by["daemon"] == "warn" and by["database"] == "warn"


def test_corrupt_db_fails_and_json_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    from taskqueue.config import db_path

    sqlite3.connect(db_path(tmp_path)).close()
    db_path(tmp_path).write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)  # header ok, pages garbage
    (tmp_path / "projects.yaml").write_text("projects:\n  p:\n    root: /nonexistent/xyz\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_home", lambda: tmp_path)
    rc = cli.main(["doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    by = {c["name"]: c for c in out["checks"]}
    assert rc == 1 and by["database"]["level"] == "fail"
    assert by["projects"]["level"] == "warn" and "nonexistent" in by["projects"]["detail"]


def test_cuda_health_does_not_import_torch_unless_asked(monkeypatch) -> None:
    import importlib

    from taskqueue import resources

    seen = []
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *a: seen.append(name) or real(name, *a))
    resources.cuda_health()
    assert "torch" not in seen


def test_top_ram_consumers_respects_time_budget() -> None:
    from taskqueue.resources import top_ram_consumers

    stats: dict = {}
    rows = top_ram_consumers(n=3, budget_sec=1e-9, stats=stats)
    assert stats.get("partial") is True and isinstance(rows, list)
