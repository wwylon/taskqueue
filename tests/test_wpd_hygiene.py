"""WP-D: yaml_patch validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskqueue.yaml_patch import patch_task_yaml


def test_yaml_patch_rejects_newlines_and_bad_types(tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text("task_id: t\npriority: 50\nenabled: false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="newlines"):
        patch_task_yaml(path, {"priority": "1\ninjected: true"})
    with pytest.raises(ValueError, match="integer"):
        patch_task_yaml(path, {"priority": "high"})
    with pytest.raises(ValueError, match="boolean"):
        patch_task_yaml(path, {"enabled": "yes"})
    assert patch_task_yaml(path, {"priority": 10, "enabled": True}) == ["priority", "enabled"]
    text = path.read_text(encoding="utf-8")
    assert "priority: 10" in text
    assert "enabled: true" in text
