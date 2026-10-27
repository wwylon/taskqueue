"""Join-file bootstrap: config-master + setup --from."""
from __future__ import annotations

from pathlib import Path

import yaml

from taskqueue.config import load_projects, project_config_path
from taskqueue.db import open_db, set_config, get_config
from taskqueue.worker_join import (
    build_join_payload,
    load_join_file,
    write_join_file,
)
from taskqueue._cli_worker import cmd_worker_config_master


def _home_with_project(tmp_path: Path, *, remote: bool = False) -> Path:
    home = tmp_path / "tqhome"
    home.mkdir()
    root = tmp_path / "myproj"
    root.mkdir()
    (root / "experiments" / "operator_queue").mkdir(parents=True)
    projects = {
        "projects": {
            "myproj": {
                "root": str(root),
                "git_url": "git@example.com:org/myproj.git",
                "remote": remote,
            }
        }
    }
    (home / "projects.yaml").write_text(yaml.dump(projects), encoding="utf-8")
    open_db(home / "queue.db").close()
    return home


def test_build_join_payload_includes_git_url(tmp_path: Path) -> None:
    home = _home_with_project(tmp_path)
    conn = open_db(home / "queue.db")
    set_config(conn, "auth_token", "secret-token")
    conn.close()
    payload = build_join_payload(
        home, master_url="http://100.64.0.1:8765", auth_token="secret-token"
    )
    assert payload["master_url"] == "http://100.64.0.1:8765"
    assert payload["auth_token"] == "secret-token"
    assert payload["projects"]["myproj"]["git_url"] == "git@example.com:org/myproj.git"


def test_load_join_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "join.yaml"
    write_join_file(
        path,
        {
            "schema": 1,
            "protocol": 1,
            "master_url": "http://100.1.2.3:8765",
            "auth_token": "tok",
            "projects": {"p": {"git_url": "git@x/y.git"}},
        },
    )
    loaded = load_join_file(path)
    assert loaded["master_url"] == "http://100.1.2.3:8765"
    assert loaded["auth_token"] == "tok"


def test_load_join_file_rejects_missing_token(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("master_url: http://1.2.3.4:8765\n", encoding="utf-8")
    try:
        load_join_file(path)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "auth_token" in str(e)


def test_config_master_writes_file(tmp_path: Path) -> None:
    home = _home_with_project(tmp_path)
    out = tmp_path / "out" / "join.yaml"
    rc = cmd_worker_config_master(home, host="100.64.0.1", out=out)
    assert rc == 0
    assert out.is_file()
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["master_url"] == "http://100.64.0.1:8765"
    assert data["auth_token"]
    conn = open_db(home / "queue.db")
    assert get_config(conn).get("auth_token") == data["auth_token"]
    conn.close()


def test_remote_project_omitted_dispatch_defaults_any(tmp_path: Path) -> None:
    from taskqueue.config import Project
    from taskqueue.db import parse_task_yaml

    root = tmp_path / "p"
    q = root / "experiments" / "operator_queue"
    q.mkdir(parents=True)
    yaml_path = q / "fleet.yaml"
    yaml_path.write_text(
        "task_id: fleet\ncommand: python\nargs: [\"-c\", \"print(1)\"]\n"
        "wall_clock_cap_sec: 60\nresources: {cpu_cores: 1, ram_gib: 1}\n",
        encoding="utf-8",
    )
    proj = Project(name="p", root=root, queue_dir=q, remote=True)
    task = parse_task_yaml(yaml_path, proj)
    assert task.dispatch == "any"

    yaml_path.write_text(
        "task_id: pinned\ncommand: python\nargs: [\"-c\", \"print(1)\"]\n"
        "dispatch: local\nwall_clock_cap_sec: 60\n"
        "resources: {cpu_cores: 1, ram_gib: 1}\n",
        encoding="utf-8",
    )
    pinned = parse_task_yaml(yaml_path, proj)
    assert pinned.dispatch == "local"


def test_load_projects_remote_and_sync_push(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "bridge"
    root.mkdir()
    (root / "experiments" / "operator_queue").mkdir(parents=True)
    cfg_dir = project_config_path(root).parent
    cfg_dir.mkdir(parents=True)
    project_config_path(root).write_text(
        yaml.dump({"sync": {"push": ["src/**", "scripts/**"]}}),
        encoding="utf-8",
    )
    (home / "projects.yaml").write_text(
        yaml.dump(
            {
                "projects": {
                    "bridge": {
                        "root": str(root),
                        "git_url": "git@x/bridge.git",
                        "remote": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    projs = load_projects(home)
    assert projs["bridge"].remote is True
    assert projs["bridge"].sync_push == ("src/**", "scripts/**")
