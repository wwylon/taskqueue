from types import SimpleNamespace

from taskqueue import scheduler
from taskqueue.scheduler import FilterResult


def test_paused_project_skips_service_probe(monkeypatch):
    """A paused/frozen project must be rejected before any (slow) service probe."""
    ok = lambda *a, **k: FilterResult.accept()  # noqa: E731
    for name in (
        "filter_draining",
        "filter_project_allowed",
        "filter_dispatch",
        "filter_requires",
        "filter_trust",
    ):
        monkeypatch.setattr(scheduler, name, ok)
    monkeypatch.setattr(
        scheduler, "filter_freeze_pause", lambda ctx: FilterResult.reject("paused", "tq.filter.paused")
    )
    probed = []
    monkeypatch.setattr(scheduler, "filter_services", lambda ctx: probed.append(1) or FilterResult.accept())

    res = scheduler.filter_task(
        {}, SimpleNamespace(stable=True), SimpleNamespace(), cpu=1, ram=1, gpu=0
    )
    assert not res.ok
    assert probed == []
