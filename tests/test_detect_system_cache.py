from taskqueue import resources


def test_raw_probe_does_not_poison_shared_cache() -> None:
    resources._SYS_CACHE = (0.0, {})
    raw = resources.detect_system(apply_last_good=False)
    assert resources._SYS_CACHE[1] == {}  # raw result never cached
    smooth = resources.detect_system()
    assert resources._SYS_CACHE[1]
    smooth["marker"] = 1
    assert "marker" not in resources.detect_system()  # callers get copies
    assert isinstance(raw, dict)
