from taskqueue.scheduler import exclusive_drain_target


def _row(excl=False, excl_proj=False):
    return {"project": "p", "task_id": "t", "exclusive": excl, "exclusive_project": excl_proj}


def test_global_exclusive_drains_everything() -> None:
    assert exclusive_drain_target(_row(excl=True), 2, {"p": 2}) == (("p", "t"), None)


def test_exclusive_project_scoped() -> None:
    assert exclusive_drain_target(_row(excl_proj=True), 3, {"p": 1}) == (("p", "t"), "p")
    assert exclusive_drain_target(_row(excl_proj=True), 3, {"q": 3}) is None


def test_no_drain_when_idle_or_not_exclusive() -> None:
    assert exclusive_drain_target(_row(excl=True), 0, {}) is None
    assert exclusive_drain_target(_row(), 5, {"p": 5}) is None
    assert exclusive_drain_target(None, 5, {}) is None
