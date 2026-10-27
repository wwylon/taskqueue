from taskqueue import daemon


class _Conn:
    rolled = False

    def rollback(self):
        self.rolled = True


def test_recovery_rolls_back_and_reaps(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(daemon, "_collect_finished_runs", lambda *a, **k: calls.append(a))
    c = _Conn()
    daemon._recover_after_tick_failure(c, {"k": 1}, None, set())
    assert c.rolled and len(calls) == 1


def test_recovery_swallows_reap_errors(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(daemon, "_collect_finished_runs", boom)
    daemon._recover_after_tick_failure(_Conn(), {}, None, None)
