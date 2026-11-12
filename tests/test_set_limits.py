"""Set limits: subtree totals (set_caps) — only min_tasks is inherited per member."""

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
    # set max_ram_gib is a subtree total, not copied into each member
    assert eff["max_ram_gib"] == 0 and src["max_ram_gib"] == "none"


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
    sets["games"]["min_tasks"] = 1
    sets["games/card"]["min_tasks"] = 2
    eff, src = resolve_effective_limits({}, "games/card", sets)
    assert eff["max_tasks"] == 0  # totals are enforced by SetCapChecker, not per member
    assert eff["min_tasks"] == 2 and src["min_tasks"] == "set:games/card"


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
    assert get_effective_project_limits(db, "whist", "games/card")["max_tasks"] == 0.0
    assert get_effective_project_limits(db, "whist", "games/card")["max_ram_gib"] == 0.0
    payload = build_limits_api_payload(db, load_projects(home))
    assert payload["projects"]["pokerbot"]["override"]["max_tasks"] == 1.0
    assert payload["projects"]["pokerbot"]["effective"]["max_tasks"] == 1.0
    assert payload["projects"]["whist"]["inherited_from"]["max_tasks"] == "none"
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
    assert body["projects"]["a"]["effective"]["max_tasks"] == 0.0  # games total, not per project
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
    assert body["projects"]["b"]["effective"]["max_tasks"] == 0.0
    db.close()


def test_set_caps_are_subtree_totals() -> None:
    from tqdaemon.set_caps import SetCapChecker

    paths = {"addbot": "bots", "bridge2": "bots/bridge2", "bridge2-htn": "bots/bridge2", "med_mr": "med"}
    sets = {"bots": {"max_tasks": 2, "max_ram_gib": 10}, "bots/bridge2": {"max_tasks": 1}}
    ck = SetCapChecker(sets, paths)
    running = {"addbot": 1, "med_mr": 5}
    zero: dict = {}
    assert ck.check("bridge2", running, zero, zero, zero, cpu=1, ram=1, gpu=0) is None
    running["bridge2"] = 1
    reason = ck.check("bridge2-htn", running, zero, zero, zero, cpu=1, ram=1, gpu=0)
    assert reason and "bots" in reason  # bots total (2) and bots/bridge2 (1) both full
    assert ck.check("med_mr", running, zero, zero, zero, cpu=1, ram=1, gpu=0) is None  # other subtree
    ram = {"addbot": 9.5}
    assert "max_ram_gib" in (ck.check("addbot", {}, zero, ram, zero, cpu=1, ram=1, gpu=0) or "")
    assert ck.over_cap({"addbot": 2, "bridge2": 1}) == [("bots", 1)]


def test_local_admit_respects_set_total(tmp_path: Path) -> None:
    """Two bot projects, bots max_tasks=1: only one bot task may start."""
    from tqdaemon.set_caps import SetCapChecker

    ck = SetCapChecker({"bots": {"max_tasks": 1}}, {"a": "bots", "b": "bots"})
    assert ck.check("b", {"a": 1}, {}, {}, {}, cpu=1, ram=0, gpu=0)
    assert ck.check("b", {}, {}, {}, {}, cpu=1, ram=0, gpu=0) is None


def test_rename_set_moves_limits_but_not_lookalike_sets(tmp_path: Path) -> None:
    from tqdaemon.project_sets import rename_set_prefix

    home = tmp_path
    _write_registry(home, {"a": {"root": str(home / "a"), "set": "bots/x"}})
    db = open_db(home / "queue.db")
    set_set_limit(db, "bots", "max_tasks", 4)
    set_set_limit(db, "bots/x", "max_ram_gib", 8)
    set_set_limit(db, "bots_other", "max_tasks", 9)  # must not move with "bots"
    db.close()
    assert rename_set_prefix(home, "bots", "robots") == ["a"]
    db = open_db(home / "queue.db")
    lim = get_all_set_limits(db)
    assert lim["robots"]["max_tasks"] == 4 and lim["robots/x"]["max_ram_gib"] == 8
    assert "bots" not in lim and lim["bots_other"]["max_tasks"] == 9
    db.close()


def test_api_explicit_nodes_carry_limits_and_usage(tmp_path: Path) -> None:
    from tqdaemon.config import Project

    root = tmp_path / "a"
    (root / "experiments" / "operator_queue").mkdir(parents=True)
    projects = {"a": Project(name="a", root=root, queue_dir=root / "experiments" / "operator_queue", set_path="med")}
    HandlerClass, db, _home = make_handler_env(tmp_path, projects=projects)
    status, body = http_request(HandlerClass, "POST", "/api/project-sets/node", {"action": "add", "path": "bots/x"})
    assert status == 200 and body["ok"] is True
    http_request(HandlerClass, "POST", "/api/set-limits", {"set": "bots", "max_tasks": 3})
    status, body = http_request(HandlerClass, "GET", "/api/project-sets")
    assert status == 200
    flat = body["flat"]
    assert "bots" in flat and "bots/x" in flat and "med" in flat
    assert flat["bots"]["limits"]["max_tasks"] == 3.0 and flat["bots"]["running"] == 0
    assert flat["bots/x"]["explicit"] is True
    status, body = http_request(HandlerClass, "POST", "/api/project-sets/node", {"action": "remove", "path": "med"})
    assert status == 409  # still has projects
    status, body = http_request(HandlerClass, "POST", "/api/project-sets/node", {"action": "remove", "path": "bots/x"})
    assert status == 200
    db.close()


def test_project_sets_report_why_waiting_and_gpu_hours(tmp_path: Path) -> None:
    from tqdaemon.config import Project

    root = tmp_path / "a"
    (root / "experiments" / "operator_queue").mkdir(parents=True)
    projects = {"a": Project(name="a", root=root, queue_dir=root / "experiments" / "operator_queue", set_path="bots")}
    HandlerClass, db, _home = make_handler_env(tmp_path, projects=projects)
    for tid, code in (("t1", "tq.block.resources"), ("t2", "tq.block.resources"), ("t3", "")):
        db.execute(
            "INSERT INTO tasks (project, task_id, status, command, args, enqueued_at, updated_at, last_error_code) "
            "VALUES ('a', ?, 'queued', 'x', '[]', '2026-01-01', '2026-01-01', ?)",
            (tid, code),
        )
    db.commit()
    status, body = http_request(HandlerClass, "GET", "/api/project-sets")
    node = body["flat"]["bots"]
    assert status == 200 and node["queued"] == 3
    assert node["waiting"][0] == {"code": "tq.block.resources", "n": 2}
    assert {"code": "queued", "n": 1} in node["waiting"]
    assert node["gpu_hours_today"] == 0.0
    db.close()
