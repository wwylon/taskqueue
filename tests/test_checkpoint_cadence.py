"""Unit tests for tq_client.CheckpointCadence (no daemon)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CLIENTS = Path(__file__).resolve().parents[1] / "clients" / "python"
sys.path.insert(0, str(_CLIENTS))

from tq_client import CheckpointCadence  # noqa: E402


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_work_trigger_at_n_not_before() -> None:
    clock = FakeClock()
    c = CheckpointCadence(every_n_units=3, every_sec=600.0, clock=clock)
    assert c.note_work() is False
    assert c.note_work() is False
    assert c.note_work() is True
    assert c.due() is True  # peek stays True until reset


def test_time_trigger_via_fake_clock_with_zero_new_work() -> None:
    clock = FakeClock(0.0)
    c = CheckpointCadence(every_n_units=1000, every_sec=10.0, clock=clock)
    assert c.due() is False
    clock.t = 10.0
    assert c.due() is True
    assert c.note_work(0) is True


def test_both_due_at_once_one_true_then_reset_clears_both() -> None:
    clock = FakeClock(0.0)
    c = CheckpointCadence(every_n_units=2, every_sec=5.0, clock=clock)
    c.note_work()
    clock.t = 5.0
    assert c.note_work() is True  # work and time both met
    c.reset()
    assert c.due() is False
    assert c.note_work() is False


def test_note_work_does_not_reset_second_call_stays_true() -> None:
    clock = FakeClock()
    c = CheckpointCadence(every_n_units=1, every_sec=600.0, clock=clock)
    assert c.note_work() is True
    assert c.note_work() is True  # still due; no implicit reset


def test_reset_after_write_clears_work_and_time() -> None:
    clock = FakeClock(0.0)
    c = CheckpointCadence(every_n_units=2, every_sec=10.0, clock=clock)
    assert c.note_work(2) is True
    clock.t = 10.0
    assert c.due() is True
    c.reset()
    assert c.due() is False
    clock.t = 19.9
    assert c.due() is False
    clock.t = 20.0
    assert c.due() is True


def test_rejects_invalid_ctor_args() -> None:
    with pytest.raises(ValueError):
        CheckpointCadence(every_n_units=0)
    with pytest.raises(ValueError):
        CheckpointCadence(every_n_units=1, every_sec=0.0)


def test_progress_loop_throttles_and_does_not_materialise_iterators(capsys, monkeypatch):
    import tq_client

    reads = []
    monkeypatch.setattr(tq_client, "maybe_suspend", lambda: reads.append(1))
    out = list(tq_client.progress_loop(range(1000), msg="m", min_interval_sec=3600))
    assert out == list(range(1000))
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["m (0.0%)", "m (100.0%)"] and len(reads) == 1  # first item + final line only

    gen = (i for i in range(3))
    assert list(tq_client.progress_loop(gen, min_interval_sec=0)) == [0, 1, 2]
    assert capsys.readouterr().out == ""  # unknown length: no percent lines


def test_python_client_lines_match_runner_parsers(capsys):
    """Shared protocol: what the client prints is what the runner parses."""
    import json

    import tq_client

    from taskqueue.runner import _PCT_RE, _TQ_METRIC_RE

    tq_client.progress(12.5, "step")
    tq_client.metric(loss=0.5, epoch=2)
    pct_line, metric_line = capsys.readouterr().out.splitlines()
    assert float(_PCT_RE.search(pct_line).group(1)) == 12.5
    assert json.loads(_TQ_METRIC_RE.match(metric_line).group(1)) == {"loss": 0.5, "epoch": 2}
