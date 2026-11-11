"""Set-level limit inheritance (override → nearest set → unlimited)."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.helpers.master_worker import http_request, make_handler_env
from tqdaemon.config import load_projects
from tqdaemon.db import open_db, set_project_limit, set_set_limit
from tqdaemon.project_limits import (
    build_limits_api_payload,
    get_all_set_limits,
    get_effective_project_limits,
    resolve_effective_limits,
    set_path_prefixes,
)


def _write_registry(home: Path, projects: dict) -> None:
    (home / "projects.yaml").write_text(
        yaml.dump({"projects": projects}),
        encoding="utf-8",
    )


def test_set_path_prefixes() -> None:
    assert set_path_prefixes("games/card") == ["games/card", "games"]
    assert set_path_prefixes(None) == []


def test_resolve_effective_override_beats_set() -> None:
    sets = {
        "games": {
            "max_tasks": 2,
            "min_tasks": 0,
            "max_cpu_cores": 0,
            "max_ram_gib": 0,
            "max_gpu_vram_gib": 0,
            "max_gpu_hours_day": 0,
        },
        "games/card": {
            "max_tasks": 5,
            "min_tasks": 0,
            "max_cpu_cores": 0,
            "max_ram_gib": 8,
            "max_gpu_vram_gib": 0,
            "max_gpu_hours_day": 0,
        },
    }
    eff, src = resolve_effective_limits({"max_tasks": 1}, "games/card", sets)
    assert eff["max_tasks"] == 1 and src["max_tasks"] == "project"
    assert eff["max_ram_gib"] == 8 and src["max_ram_gib"] == "set:games/card"


def test_resolve_nearer_set_beats_farther() -> None:
    sets = {
        "games": {
            "max_tasks": 2,
            "min_tasks": 0,
            "max_cpu_cores": 0,
            "max_ram_gib": 0,
            "max_gpu_vram_gib": 0,
            "max_gpu_hours_day": 0,
        },
        "games/card": {
            "max_tasks": 9,
            "min_tasks": 0,
            "max_cpu_cores": 0,
            "max_ram_gib": 0,
            "max_gpu_vram_gib": 0,
            "max_gpu_hours_day": 0,
        },
    }
    eff, src = resolve_effective_limits({}, "games/card", sets)
    assert eff["max_tasks"] == 9 and src["max_tasks"] == "set:games/card"


def test_set_limit_roundtrip_and_effective(tmp_path: Path) -> None:
    home = tmp_path
    _write_registry(
        home,
        {
            "pokerbot": {"root": str(home / "p"), "set": "games/card"},
            "whist": {"root": str(home / "w"), "set": "games/card"},
        },
    )
    db = open_db(home / "queue.db")
    set_set_limit(db, "games", "max_tasks", 3)
    set_set_limit(db, "games/card", "max_ram_gib", 4)
    set_project_limit(db, "pokerbot", "max_tasks", 1)
    assert get_all_set_limits(db)["games"]["max_tasks"] == 3.0
    assert get_effective_project_limits(db, "pokerbot", "games/card")["max_tasks"] == 1.0
    assert get_effective_project_limits(db, "whist", "games/card")["max_tasks"] == 3.0
    assert get_effective_project_limits(db, "whist", "games/card")["max_ram_gib"] == 4.0
    payload = build_limits_api_payload(db, load_projects(home))
    assert payload["projects"]["pokerbot"]["override"]["max_tasks"] == 1.0
    assert payload["projects"]["pokerbot"]["effective"]["max_tasks"] == 1.0
    assert payload["projects"]["whist"]["inherited_from"]["max_tasks"] == "set:games"
    assert payload["sets"]["games"]["max_tasks"] == 3.0
    db.close()


def test_api_set_limits(tmp_path: Path) -> None:
    from tqdaemon.config import Project

    home = tmp_path
    root_a = home / "a"
    root_b = home / "b"
    for root in (root_a, root_b):
        (root / "experiments" / "operator_queue").mkdir(parents=True)
    projects = {
        "a": Project(
            name="a",
            root=root_a,
            queue_dir=root_a / "experiments" / "operator_queue",
            set_path="games/card",
        ),
        "b": Project(
            name="b",
            root=root_b,
            queue_dir=root_b / "experiments" / "operator_queue",
            set_path="games/card",
        ),
    }
    HandlerClass, db, _home = make_handler_env(tmp_path, projects=projects)
    status, body = http_request(HandlerClass, "POST", "/api/set-limits", {"set": "games", "max_tasks": 2})
    assert status == 200 and body.get("ok") is True
    status, body = http_request(HandlerClass, "GET", "/api/project-limits")
    assert status == 200
    assert body["sets"]["games"]["max_tasks"] == 2.0
    assert body["projects"]["a"]["effective"]["max_tasks"] == 2.0
    status, body = http_request(
        HandlerClass,
        "POST",
        "/api/project-limits/a",
        {
            "max_tasks": 7,
            "min_tasks": 0,
            "max_cpu_cores": 0,
            "max_ram_gib": 0,
            "max_gpu_vram_gib": 0,
            "max_gpu_hours_day": 0,
        },
    )
    assert status == 200
    status, body = http_request(HandlerClass, "GET", "/api/project-limits")
    assert body["projects"]["a"]["effective"]["max_tasks"] == 7.0
    assert body["projects"]["b"]["effective"]["max_tasks"] == 2.0
    db.close()
