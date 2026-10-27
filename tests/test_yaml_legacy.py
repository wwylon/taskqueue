"""Legacy YAML key hints and tq validate --fix."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from taskqueue.config import Project
from taskqueue.db import parse_task_yaml, parse_task_yaml_many
from taskqueue.validate import validate_file, validate_home
from taskqueue.yaml_legacy import (
    LEGACY_KEY_HINTS,
    fix_legacy_keys,
    fix_legacy_yaml_file,
    suggest_unknown_key,
)


_KNOWN = frozenset({
    "task_id", "command", "description", "resources", "wall_clock_cap_sec",
    "metadata",
})


def test_legacy_hints_cover_five_keys() -> None:
    assert set(LEGACY_KEY_HINTS) == {
        "ram_gib", "timeout_s", "hypothesis", "project", "name",
    }
    for uk, hint in LEGACY_KEY_HINTS.items():
        line = suggest_unknown_key(uk, _KNOWN)
        assert uk in line
        assert "x-ram_gib" not in line
        assert hint.split("(")[0].strip()[:12] in line or "resources" in line or "omit" in line


def test_suggest_unknown_falls_back_to_x_prefix() -> None:
    line = suggest_unknown_key("totally_made_up", _KNOWN)
    assert "x-totally_made_up" in line


def test_suggest_close_match() -> None:
    line = suggest_unknown_key("wall_clock_cap", _KNOWN)
    assert "wall_clock_cap_sec" in line


def test_fix_legacy_keys_all_rules() -> None:
    raw = {
        "task_id": "t1",
        "command": "python",
        "project": "demo",
        "ram_gib": 4,
        "timeout_s": 3600,
        "hypothesis": "H1",
        "name": "Nice name",
    }
    out, changes = fix_legacy_keys(raw, project_name="demo")
    assert "project" not in out
    assert "ram_gib" not in out
    assert out["resources"]["ram_gib"] == 4
    assert out["wall_clock_cap_sec"] == 3600
    assert "timeout_s" not in out
    assert out["x-hypothesis"] == "H1"
    assert "hypothesis" not in out
    assert out["description"] == "Nice name"
    assert "name" not in out
    assert len(changes) == 5


def test_fix_keeps_existing_cap_and_resources_ram() -> None:
    raw = {
        "task_id": "t1",
        "ram_gib": 2,
        "timeout_s": 100,
        "resources": {"ram_gib": 8},
        "wall_clock_cap_sec": 999,
    }
    out, changes = fix_legacy_keys(raw, project_name="demo")
    assert out["resources"]["ram_gib"] == 8
    assert out["wall_clock_cap_sec"] == 999
    assert any("already set" in c for c in changes)


def test_fix_name_to_x_name_when_description_set() -> None:
    raw = {"task_id": "t", "description": "keep", "name": "other"}
    out, _ = fix_legacy_keys(raw, project_name="demo")
    assert out["description"] == "keep"
    assert out["x-name"] == "other"


def test_fix_mismatched_project_raises() -> None:
    with pytest.raises(ValueError, match="does not match"):
        fix_legacy_keys({"project": "other"}, project_name="demo")


def test_parse_task_yaml_legacy_hint_in_error(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        "task_id: bad\ncommand: python\nram_gib: 4\ntimeout_s: 10\n",
        encoding="utf-8",
    )
    proj = Project(name="demo", root=tmp_path, queue_dir=tmp_path)
    with pytest.raises(ValueError, match="resources.ram_gib") as ei:
        parse_task_yaml(p, proj)
    msg = str(ei.value)
    assert "wall_clock_cap_sec" in msg
    assert "x-ram_gib" not in msg


def test_fix_legacy_yaml_file_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "job.yaml"
    p.write_text(
        yaml.dump({
            "task_id": "job",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "project": "demo",
            "hypothesis": "probe",
            "ram_gib": 2,
        }),
        encoding="utf-8",
    )
    changes = fix_legacy_yaml_file(p, project_name="demo")
    assert changes
    proj = Project(name="demo", root=tmp_path, queue_dir=tmp_path)
    task = parse_task_yaml(p, proj)
    assert task.task_id == "job"
    assert task.resources.ram_gib == 2.0
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert raw.get("x-hypothesis") == "probe"
    assert "project" not in raw


def test_validate_fix_rewrites_then_ok(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "proj"
    q = root / "experiments" / "operator_queue"
    q.mkdir(parents=True)
    (q / "legacy.yaml").write_text(
        yaml.dump({
            "task_id": "legacy",
            "command": sys.executable,
            "args": ["-c", "pass"],
            "project": "demo",
            "ram_gib": 3,
            "timeout_s": 120,
            "hypothesis": "x",
        }),
        encoding="utf-8",
    )
    (home / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"root": str(root)}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    report = validate_home(home, fix=True)
    assert report["valid"]
    assert report["ok"] >= 1
    assert len(report["fixed"]) == 1
    raw = yaml.safe_load((q / "legacy.yaml").read_text(encoding="utf-8"))
    assert "project" not in raw
    assert raw["resources"]["ram_gib"] == 3
    assert raw["wall_clock_cap_sec"] == 120
    assert raw["x-hypothesis"] == "x"


def test_validate_file_fix_mismatched_project(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "proj"
    q = root / "experiments" / "operator_queue"
    q.mkdir(parents=True)
    path = q / "bad.yaml"
    path.write_text(
        yaml.dump({
            "task_id": "bad",
            "command": sys.executable,
            "project": "wrong",
        }),
        encoding="utf-8",
    )
    (home / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"root": str(root)}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TASKQUEUE_HOME", str(home))
    report = validate_file(path, home, fix=True)
    assert not report["valid"]
    assert any("does not match" in e["error"] for e in report["errors"])


def test_multi_doc_yaml_parse_task_yaml_many(tmp_path: Path) -> None:
    """parse_task_yaml_many returns one task per --- document."""
    p = tmp_path / "multi.yaml"
    p.write_text(
        "task_id: first\ncommand: echo\nargs: [a]\n"
        "---\n"
        "task_id: second\ncommand: echo\nargs: [b]\n",
        encoding="utf-8",
    )
    proj = Project(name="demo", root=tmp_path, queue_dir=tmp_path)
    tasks = parse_task_yaml_many(p, proj)
    assert len(tasks) == 2
    assert tasks[0].task_id == "first"
    assert tasks[1].task_id == "second"


def test_multi_doc_yaml_parse_task_yaml_raises(tmp_path: Path) -> None:
    """parse_task_yaml rejects multi-document files with a clear error."""
    p = tmp_path / "multi.yaml"
    p.write_text(
        "task_id: first\ncommand: echo\n"
        "---\n"
        "task_id: second\ncommand: echo\n",
        encoding="utf-8",
    )
    proj = Project(name="demo", root=tmp_path, queue_dir=tmp_path)
    with pytest.raises(ValueError, match="2 YAML documents"):
        parse_task_yaml(p, proj)


def test_multi_doc_legacy_fixer_raises(tmp_path: Path) -> None:
    """fix_legacy_yaml_file rejects multi-document files."""
    p = tmp_path / "multi.yaml"
    p.write_text(
        "task_id: first\ncommand: echo\nproject: demo\n"
        "---\n"
        "task_id: second\ncommand: echo\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not support multi-document"):
        fix_legacy_yaml_file(p, project_name="demo")


def test_single_doc_with_leading_separator(tmp_path: Path) -> None:
    """A file starting with --- (valid YAML) still parses as one task."""
    p = tmp_path / "leading.yaml"
    p.write_text(
        "---\ntask_id: only\ncommand: echo\n",
        encoding="utf-8",
    )
    proj = Project(name="demo", root=tmp_path, queue_dir=tmp_path)
    task = parse_task_yaml(p, proj)
    assert task.task_id == "only"
    tasks = parse_task_yaml_many(p, proj)
    assert len(tasks) == 1
