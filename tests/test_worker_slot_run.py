"""WorkerSlot.run end to end with no reachable master: master-dependent phases degrade, the task runs."""
from __future__ import annotations

import pytest

import sys
import threading
from pathlib import Path

from taskqueue.worker_slot import WorkerSlot


@pytest.mark.slow
def test_slot_runs_task_and_finalizes_without_master(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    root = tmp_path / "proj"
    home.mkdir()
    root.mkdir()
    (root / "reports").mkdir()
    (home / "projects.yaml").write_text(f"projects:\n  p:\n    root: {root.as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    script = root / "job.py"
    script.write_text("import os\nprint('(50.0%) half', flush=True)\nprint(os.environ['TQ_PROJECT'])\n", encoding="utf-8")

    finals: list[dict] = []
    chunks: list[str] = []
    slots = {7: {}}
    slot = WorkerSlot(
        home=home, master_url="http://127.0.0.1:9", worker_id="w1", session_id="s1",
        finalize_fn=lambda run_id, **kw: finals.append({"run_id": run_id, **kw}),
        log_chunk_fn=lambda run_id, text: chunks.append(text),
        slots_lock=threading.Lock(), slots=slots,
    )
    resp = {
        "finalize_token": "tok",
        "task": {
            "project": "p", "task_id": "t", "command": sys.executable, "args": [str(script)],
            "requires_paths": [], "env": '{"TQ_PROJECT": "hijack"}',
        },
    }
    sl = {"stop_flag": [False], "control_file": [None], "proc": [None]}
    slot.run(resp, sl, 7)

    assert finals and finals[-1]["status"] == "completed" and finals[-1]["exit_code"] == 0
    out = "".join(chunks)
    assert "(50.0%) half" in out
    assert "\np\n" in "\n" + out and "hijack" not in out  # task env cannot override TQ_PROJECT
    assert 7 not in slots  # slot released
