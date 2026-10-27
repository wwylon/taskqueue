"""GPU task badges, sidecar VRAM attribution, and log issue counts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskqueue.log_issues import (
    apply_log_lines_to_metrics,
    apply_log_lines_to_metrics_json,
    classify_log_line,
    format_log_counts,
    merge_metrics_payload,
)
from taskqueue.resources import (
    attributed_gpu_pids,
    attributed_vram_gib,
    sidecar_name_hints,
    task_requires_gpu,
)


def test_task_requires_gpu() -> None:
    assert task_requires_gpu({"gpu_vram_gib": 4.3}) is True
    assert task_requires_gpu({"gpu_vram_gib": 0}) is False
    assert task_requires_gpu({"gpu_vram_gib": 0.0}) is False
    assert task_requires_gpu({}) is False
    assert task_requires_gpu(None) is False
    assert task_requires_gpu('{"gpu_vram_gib": 1.5}') is True


def test_generic_sidecar_hints_ignored() -> None:
    assert sidecar_name_hints({"locks": ["python"]}) == []
    assert sidecar_name_hints({"locks": ["node", "java", "ab"]}) == []
    assert sidecar_name_hints({"locks": ["ollama"]}) == ["ollama"]
    assert sidecar_name_hints(
        {"locks": ["ollama"]},
        [{"name": "vllm"}, {"name": "python"}],
    ) == ["ollama", "vllm"]


def test_sidecar_vram_attribution() -> None:
    procs = [
        {"pid": 1, "name": "python.exe", "cmdline_short": "python train.py", "vram_gib": 1.0},
        {"pid": 2, "name": "ollama.exe", "cmdline_short": "ollama serve", "vram_gib": 4.3},
        {"pid": 3, "name": "python.exe", "cmdline_short": "python other.py", "vram_gib": 2.0},
    ]
    hints = sidecar_name_hints({"locks": ["ollama"], "gpu_vram_gib": 4.3})
    attributed = attributed_gpu_pids(procs, tree_pids={1}, hints=hints)
    assert {p["pid"] for p in attributed} == {1, 2}
    assert attributed_vram_gib(procs, {1}, hints) == pytest.approx(5.3)

    # Other CUDA python is not counted without a tree pid or sidecar hint.
    other_only = attributed_gpu_pids(procs, tree_pids=set(), hints=hints)
    assert {p["pid"] for p in other_only} == {2}


def test_ollama_lock_matches_llama_server() -> None:
    """Windows Ollama CUDA app is llama-server.exe, not ollama.exe."""
    procs = [
        {
            "pid": 9792,
            "name": "llama-server.exe",
            "smi_name": r"C:\Users\example\AppData\Local\Programs\Ollama\lib\ollama\llama-server.exe",
            "cmdline_short": "",
            "vram_gib": 0.0,
            "vram_unknown": True,
        }
    ]
    hints = sidecar_name_hints({"locks": ["ollama"]})
    attributed = attributed_gpu_pids(procs, tree_pids=set(), hints=hints)
    assert [p["pid"] for p in attributed] == [9792]
    assert attributed_vram_gib(procs, set(), hints) == 0.0
    assert attributed_vram_gib(procs, set(), hints, device_used_gib=5.405) == pytest.approx(5.405)

    # Device used must not be charged to an unrelated process.
    other = [{"pid": 1, "name": "chrome.exe", "vram_gib": 0.0, "vram_unknown": True}]
    assert attributed_vram_gib(other, set(), hints, device_used_gib=5.405) == 0.0


def test_numeric_sidecar_vram_not_replaced_by_device_used() -> None:
    procs = [
        {"pid": 2, "name": "ollama.exe", "cmdline_short": "ollama serve", "vram_gib": 4.3},
    ]
    hints = ["ollama"]
    assert attributed_vram_gib(procs, set(), hints, device_used_gib=8.0) == pytest.approx(4.3)


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("WARN: timed out", "warnings"),
        ("[queue] WARN", "warnings"),
        ("WARNING: slow", "warnings"),
        ("REJECT: low quality", "errors"),
        ("ERROR: boom", "errors"),
        ("Error: file missing", "errors"),
        ("FATAL cannot continue", "errors"),
        ("CRITICAL: disk", "errors"),
        ("step LOW SCORE locale=de", "other"),
        ("[197/512] translating", None),
        ("0 errors", None),
        ("an error occurred", None),
        ("failed to connect", None),
        ("\x1b[31mWARN: colored\x1b[0m", "warnings"),
        ("", None),
    ],
)
def test_classify_log_line(line: str, kind: str | None) -> None:
    assert classify_log_line(line) == kind


def test_apply_log_lines_to_metrics_json() -> None:
    chunk = (
        "WARN: timed out\n"
        "[queue] WARN ollama down\n"
        "REJECT: skip\n"
        "LOW SCORE\n"
        "(47.0%)\n"
        "0 errors\n"
    )
    out = json.loads(apply_log_lines_to_metrics_json(None, chunk.splitlines()))
    assert out["log"] == {"warnings": 2, "errors": 1, "other": 1}


def test_tq_metric_cannot_overwrite_log() -> None:
    payload = merge_metrics_payload({"acc": 0.9, "log": {"errors": 99}}, {"warnings": 3, "errors": 1, "other": 0})
    assert payload["log"] == {"warnings": 3, "errors": 1, "other": 0}
    assert payload["acc"] == 0.9
    assert format_log_counts(payload["log"]) == "warn=3 err=1 other=0"
    assert format_log_counts({"warnings": 0, "errors": 0, "other": 0}) is None


def test_apply_log_lines_preserves_task_metrics() -> None:
    merged = apply_log_lines_to_metrics({"acc": 0.5}, ["ERROR: x"])
    assert merged["acc"] == 0.5
    assert merged["log"]["errors"] == 1


def test_build_run_metrics_requires_gpu_and_log(tmp_path: Path) -> None:
    import time

    from taskqueue.db import open_db
    from taskqueue.metrics_snapshot import build_run_metrics
    from tests.helpers.master_worker import seed_queued_task

    db = open_db(tmp_path / "q.db")
    seed_queued_task(
        db,
        resources={"cpu_cores": 1, "ram_gib": 1.0, "gpu_vram_gib": 4.3, "locks": ["ollama"]},
    )
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    db.execute("UPDATE tasks SET status='running' WHERE task_id='T1'")
    db.execute(
        "INSERT INTO runs (project, task_id, status, started_at, progress_pct, "
        "peak_cpu_pct, peak_ram_gib, peak_vram_gib, metrics_json) "
        "VALUES ('proj', 'T1', 'running', ?, 47.0, 10.0, 1.0, 4.2, ?)",
        (now, json.dumps({"log": {"warnings": 12, "errors": 3, "other": 5}})),
    )
    db.commit()
    m = build_run_metrics(db, "proj", "T1", home=tmp_path, samples=10)
    assert m["requires_gpu"] is True
    assert m["log"] == {"warnings": 12, "errors": 3, "other": 5}
    db.close()
