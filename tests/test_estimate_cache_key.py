from taskqueue import estimate


def test_declared_change_busts_cache_and_cache_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        estimate, "predict_task_resources",
        lambda *a, **k: {"ram_gib": 0.0, "vram_gib": 0.0, "source": estimate.SOURCE_DEFAULT},
    )
    cache: dict = {}
    a = estimate.estimate_task_resources(None, "p", "t", {"ram_gib": 4}, cache=cache)
    b = estimate.estimate_task_resources(None, "p", "t", {"ram_gib": 16}, cache=cache)
    assert a["ram_gib"] == 4 and b["ram_gib"] == 16
    monkeypatch.setattr(estimate, "ESTIMATE_CACHE_MAX", 3)
    for i in range(10):
        estimate.estimate_task_resources(None, "p", f"t{i}", {}, cache=cache)
    assert len(cache) <= 3
