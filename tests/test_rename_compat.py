"""taskqueue -> tqdaemon rename compat: new names win, legacy names keep working."""
from __future__ import annotations

from pathlib import Path

import pytest

from tqdaemon import adopt, config


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv(config.ENV_HOME_LEGACY, raising=False)
    monkeypatch.delenv(config.ENV_HOME, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_env_new_wins_over_legacy(clean_env, monkeypatch):
    monkeypatch.setenv(config.ENV_HOME_LEGACY, str(clean_env / "old"))
    assert config.state_home() == clean_env / "old"
    monkeypatch.setenv(config.ENV_HOME, str(clean_env / "new"))
    assert config.state_home() == clean_env / "new"
    assert config.env_home_value() == str(clean_env / "new")


def test_default_home_fresh_install(clean_env):
    assert config.state_home() == clean_env / config.DEFAULT_HOME_DIRNAME


def test_existing_legacy_home_is_used(clean_env):
    (clean_env / ".taskqueue").mkdir()
    assert config.state_home() == clean_env / ".taskqueue"


def test_new_home_preferred_when_both_exist(clean_env):
    (clean_env / ".taskqueue").mkdir()
    (clean_env / ".tqdaemon").mkdir()
    assert config.state_home() == clean_env / ".tqdaemon"


def test_project_config_dir_lookup(tmp_path):
    root = tmp_path / "proj"
    assert config.project_config_path(root) == root / config.DEFAULT_PROJECT_DIRNAME / "project.yaml"
    (root / ".taskqueue").mkdir(parents=True)
    (root / ".taskqueue" / "project.yaml").write_text("sync: {push: [a]}\n", encoding="utf-8")
    assert config.project_config_path(root) == root / ".taskqueue" / "project.yaml"
    assert config.load_project_file_config(root) == {"sync": {"push": ["a"]}}
    (root / ".tqdaemon").mkdir()
    (root / ".tqdaemon" / "project.yaml").write_text("sync: {push: [b]}\n", encoding="utf-8")
    assert config.project_config_path(root) == root / ".tqdaemon" / "project.yaml"
    assert config.load_project_file_config(root) == {"sync": {"push": ["b"]}}


def test_wrapper_modules_cover_both_names():
    assert adopt.WRAPPER_MODULE in adopt.WRAPPER_MODULES
    assert "taskqueue.run_wrapper" in adopt.WRAPPER_MODULES
