from taskqueue import scheduler


def _row(task_id, deps=(), project="p"):
    import json

    return {"project": project, "task_id": task_id, "depends_on": json.dumps(list(deps)), "depends_on_any": "[]"}


def test_blocked_top_task_is_skipped(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "is_project_paused", lambda c, p: p == "paused")
    status = {("p", "dep"): "queued"}
    rows = [_row("a", ["dep"]), _row("b", project="paused"), _row("c")]
    top = scheduler.pick_reservation_top(None, rows, lambda p, t: status.get((p, t)))
    assert top["task_id"] == "c"


def test_none_when_everything_blocked(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "is_project_paused", lambda c, p: False)
    rows = [_row("a", ["missing"])]
    assert scheduler.pick_reservation_top(None, rows, lambda p, t: None) is None
