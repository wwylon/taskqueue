from types import SimpleNamespace

from taskqueue.daemon import _split_dying


def test_dying_runs_excluded_and_counted_as_relief() -> None:
    dying = SimpleNamespace(stop_reason="ram_hard", current_ram_gib=4.0)
    live = SimpleNamespace(stop_reason=None, current_ram_gib=2.0)
    cand, pending = _split_dying({("p", "a"): dying, ("p", "b"): live}, "current_ram_gib")
    assert list(cand) == [("p", "b")]
    assert pending == 4.0
