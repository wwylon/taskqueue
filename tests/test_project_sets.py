"""Project-set tree: path validation, membership, rename, API subtree filter/bulk."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.helpers.master_worker import http_request, make_handler_env, seed_queued_task
from tqdaemon.config import Project, load_projects
from tqdaemon.db import is_project_frozen
from tqdaemon.project_sets import (
    UNSET_SENTINEL,
    build_tree,
    flat_nodes,
    is_under,
    normalize_path,
    projects_in_subtree,
    rename_set_prefix,
    resolve_set_scope,
    set_project_set,
)


def _write_registry(home: Path, projects: dict) -> None:
    (home / "projects.yaml").write_text(
        yaml.dump({"projects": projects}),
        encoding="utf-8",
    )


def test_normalize_path_valid_and_invalid() -> None:
    assert normalize_path(None) is None
    assert normalize_path("") is None
    assert normalize_path("  games/card  ") == "games/card"
    assert normalize_path(r"games\card") == "games/card"
    for bad in ("/games", "games/", "games//card", "games/../x", "games/bad space"):
        try:
            normalize_path(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_is_under_and_subtree(tmp_path: Path) -> None:
    assert is_under("games/card", "games")
    assert is_under("games", "games")
    assert not is_under("games", "games/card")
    assert not is_under("med", "games")

    _write_registry(
        tmp_path,
        {
            "pokerbot": {"root": str(tmp_path / "pokerbot"), "set": "games/card"},
            "whist": {"root": str(tmp_path / "whist"), "set": "games/card"},
            "board": {"root": str(tmp_path / "board"), "set": "games/board"},
            "sono": {"root": str(tmp_path / "sono"), "set": "med/imaging"},
            "lone": {"root": str(tmp_path / "lone")},
        },
    )
    projects = load_projects(tmp_path)
    assert projects["pokerbot"].set_path == "games/card"
    assert projects["lone"].set_path is None
    assert projects_in_subtree(projects, "games") == ["board", "pokerbot", "whist"]
    assert projects_in_subtree(projects, "games/card") == ["pokerbot", "whist"]
    tree = build_tree(projects)
    root_names = [c["path"] for c in tree["children"]]
    assert root_names == ["games", "med"]
    flat = flat_nodes(projects)
    assert flat["games"]["subtree_count"] == 3
    assert flat["games/card"]["direct_projects"] == ["pokerbot", "whist"]


def test_set_project_set_and_rename_prefix(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        {
            "pokerbot": {"root": str(tmp_path / "a"), "set": "games/card"},
            "whist": {"root": str(tmp_path / "b"), "set": "games/card"},
            "board": {"root": str(tmp_path / "c"), "set": "games/board"},
        },
    )
    prev = set_project_set(tmp_path, "board", "games/table")
    assert prev == "games/board"
    projects = load_projects(tmp_path)
    assert projects["board"].set_path == "games/table"

    affected = rename_set_prefix(tmp_path, "games/card", "games/poker")
    assert affected == ["pokerbot", "whist"]
    projects = load_projects(tmp_path)
    assert projects["pokerbot"].set_path == "games/poker"
    assert projects["whist"].set_path == "games/poker"

    set_project_set(tmp_path, "board", None)
    assert load_projects(tmp_path)["board"].set_path is None


def test_resolve_set_scope(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        {
            "pokerbot": {"root": str(tmp_path / "a"), "set": "games/card"},
            "sono": {"root": str(tmp_path / "b"), "set": "med"},
            "lone": {"root": str(tmp_path / "c")},
        },
    )
    projects = load_projects(tmp_path)
    proj, plist, err = resolve_set_scope(projects, set_q="games", project_q=None)
    assert err is None and proj is None and plist == ["pokerbot"]
    proj, plist, err = resolve_set_scope(projects, set_q="games", project_q="sono")
    assert err and proj is None
    proj, plist, err = resolve_set_scope(projects, set_q=UNSET_SENTINEL, project_q=None)
    assert err is None and plist == ["lone"]
    proj, plist, err = resolve_set_scope(projects, set_q="missing", project_q=None)
    assert err and "unknown" in err


def test_api_project_sets_and_bulk(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    for root in (root_a, root_b):
        (root / "experiments" / "operator_queue").mkdir(parents=True)

    _write_registry(
        tmp_path,
        {
            "pokerbot": {"root": str(root_a), "set": "games/card"},
            "whist": {"root": str(root_b), "set": "games/card"},
        },
    )
    projects = {
        "pokerbot": Project(
            name="pokerbot",
            root=root_a,
            queue_dir=root_a / "experiments" / "operator_queue",
            set_path="games/card",
        ),
        "whist": Project(
            name="whist",
            root=root_b,
            queue_dir=root_b / "experiments" / "operator_queue",
            set_path="games/card",
        ),
    }
    HandlerClass, db, home = make_handler_env(tmp_path, projects=projects)
    seed_queued_task(db, project="pokerbot", task_id="t1", dispatch="local")
    seed_queued_task(db, project="whist", task_id="t1", dispatch="local")
    db.commit()

    status, payload = http_request(HandlerClass, "GET", "/api/project-sets")
    assert status == 200
    assert "games" in payload["flat"]
    assert payload["flat"]["games"]["subtree_count"] == 2

    status, data = http_request(HandlerClass, "POST", "/api/project-set/freeze", {"set": "games"})
    assert status == 200
    assert data["ok"] is True
    assert set(data["projects"]) == {"pokerbot", "whist"}

    assert is_project_frozen(db, "pokerbot")
    assert is_project_frozen(db, "whist")

    status, q = http_request(HandlerClass, "GET", "/api/queue?set=games")
    assert status == 200
    assert q["total"] == 2

    status, bad = http_request(HandlerClass, "GET", "/api/queue?set=games&project=missing")
    assert status == 200
    assert bad.get("ok") is False


def test_cli_set_assign_tree_rename(tmp_path: Path, capsys) -> None:
    from tqdaemon._cli_project import cmd_set

    root = tmp_path / "p"
    root.mkdir()
    _write_registry(tmp_path, {"pokerbot": {"root": str(root)}})
    assert cmd_set(tmp_path, ["assign", "pokerbot", "games/card"]) == 0
    assert load_projects(tmp_path)["pokerbot"].set_path == "games/card"
    assert cmd_set(tmp_path, ["tree"]) == 0
    out = capsys.readouterr().out
    assert "games/card" in out
    assert cmd_set(tmp_path, ["rename", "games", "play"]) == 0
    assert load_projects(tmp_path)["pokerbot"].set_path == "play/card"
