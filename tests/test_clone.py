"""tq clone: a new task YAML from an existing task."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from taskqueue.clone import clone_task_yaml, parse_set_args
from taskqueue.config import load_projects
from taskqueue.db import open_db, parse_task_yaml, upsert_task


def _setup(tmp_path: Path):
    root = tmp_path / "proj"
    qdir = root / "experiments" / "operator_queue"
    qdir.mkdir(parents=True)
    (tmp_path / "projects.yaml").write_text(f"projects:\n  p:\n    root: {root.as_posix()}\n", encoding="utf-8")
    src = qdir / "train.yaml"
    src.write_text("task_id: train\ncommand: python\nargs: [train.py, --lr, '0.1']\npriority: 40\n", encoding="utf-8")
    projects = load_projects(tmp_path)
    db = open_db(tmp_path / "queue.db")
    upsert_task(db, parse_task_yaml(src, projects["p"]))
    return db, projects, qdir


def test_clone_copies_yaml_with_new_id_and_overrides(tmp_path: Path) -> None:
    db, projects, qdir = _setup(tmp_path)
    dest = clone_task_yaml(db, projects, "p", "train", "train_lr2", parse_set_args(["priority=80", "args=[train.py, --lr, '0.2']"]))
    doc = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert dest == qdir / "train_lr2.yaml"
    assert doc["task_id"] == "train_lr2" and doc["priority"] == 80 and doc["args"][-1] == "0.2"
    assert doc["command"] == "python"


def test_clone_refuses_existing_and_unknown(tmp_path: Path) -> None:
    db, projects, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        clone_task_yaml(db, projects, "p", "train", "train")
    with pytest.raises(ValueError, match="not found"):
        clone_task_yaml(db, projects, "p", "nope", "x")
    with pytest.raises(ValueError, match="key=value"):
        parse_set_args(["oops"])
