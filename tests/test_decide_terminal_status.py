"""Characterization of the terminal-status decision for a reaped local run."""

from __future__ import annotations

import itertools

import pytest

from tqdaemon import errors as tqerr
from tqdaemon.daemon import decide_terminal_status as d
from tqdaemon.models import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_STOPPED,
    STATUS_SUSPENDED,
)
from tqdaemon.stop_reasons import FORCED_STOP_REASONS, PRESSURE_STOP_REASONS, TQ_STOP_REASONS

SUSP = 75


def _d(code, reason=None, *, crashed=False, stop_req=False, neg=False):
    return d(code, reason, reader_crashed=crashed, stop_requested=stop_req, negative_exit=neg)


@pytest.mark.parametrize(
    "args, kwargs, expected",
    [
        ((0,), {}, (STATUS_COMPLETED, None, None)),
        ((SUSP, "deps_invalidated"), {}, (STATUS_STOPPED, tqerr.STOP_DEPS, "tq")),
        ((0, "deps_invalidated"), {}, (STATUS_STOPPED, tqerr.STOP_DEPS, "tq")),
        ((SUSP, "scavenger_preempt"), {}, (STATUS_STOPPED, tqerr.STOP_SCAVENGER, "tq")),
        ((0, "scavenger_preempt"), {}, (STATUS_COMPLETED, None, None)),
        ((-1,), {"crashed": True}, (STATUS_FAILED, tqerr.RUN_NONZERO, None)),
        # F-237: a crashed reader with a declared negative exit (Windows terminate() == 1).
        ((1,), {"crashed": True, "neg": True}, (STATUS_FAILED, tqerr.RUN_NONZERO, None)),
        ((0, "ram_hard"), {"crashed": True}, (STATUS_STOPPED, tqerr.STOP_RAM, "tq")),
        # F-219: pressure stops are auto-restartable whatever the exit code.
        ((0, "ram_hard"), {}, (STATUS_STOPPED, tqerr.STOP_RAM, "tq")),
        ((0, "vram_hard"), {}, (STATUS_STOPPED, tqerr.STOP_VRAM, "tq")),
        # F-219: exit 75 under a cap is a cap (auto_retry_on_cap), not a manual suspend.
        ((SUSP, "wall_clock_cap"), {}, (STATUS_INTERRUPTED, tqerr.RUN_CAP, "tq")),
        # F-219: limit_decrease_policy=evict lands auto-restartable.
        ((SUSP, "evicted"), {}, (STATUS_STOPPED, None, "tq")),
        ((0, "evicted"), {}, (STATUS_INTERRUPTED, None, "tq")),
        ((0, "wall_clock_cap"), {}, (STATUS_INTERRUPTED, tqerr.RUN_CAP, "tq")),
        ((1, "ram_hard"), {}, (STATUS_STOPPED, tqerr.STOP_RAM, "tq")),
        ((SUSP, "vram_hard"), {}, (STATUS_STOPPED, tqerr.STOP_VRAM, "tq")),
        ((SUSP,), {}, (STATUS_SUSPENDED, None, None)),
        ((SUSP,), {"stop_req": True}, (STATUS_SUSPENDED, None, None)),
        ((1, "frozen"), {}, (STATUS_INTERRUPTED, tqerr.RUN_FROZEN, "tq")),
        ((1, "wall_clock_cap"), {}, (STATUS_INTERRUPTED, tqerr.RUN_CAP, "tq")),
        ((1, "evicted"), {}, (STATUS_STOPPED, None, "tq")),
        ((1,), {"stop_req": True}, (STATUS_INTERRUPTED, None, "user")),
        ((1, "host-sleep"), {"stop_req": True}, (STATUS_INTERRUPTED, None, "host-sleep")),
        ((3,), {"neg": True}, (STATUS_COMPLETED, tqerr.RUN_NEGATIVE, None)),
        ((2,), {}, (STATUS_FAILED, tqerr.RUN_NONZERO, None)),
    ],
)
def test_decide_terminal_status(args, kwargs, expected):
    assert _d(*args, **kwargs) == expected


# --- Exhaustive sweep: policy invariants over every input combination -------------

_CODES = (0, 1, 2, -1, -9, -15, SUSP, 137, 143, 255)
_REASONS = (None, "stop_requested", "host-sleep", "made_up_reason", *sorted(TQ_STOP_REASONS))
_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_INTERRUPTED, STATUS_STOPPED, STATUS_SUSPENDED}


def _sweep():
    for code, reason, crashed, stop_req, neg in itertools.product(
        _CODES, _REASONS, (False, True), (False, True), (False, True)
    ):
        yield (code, reason, crashed, stop_req, neg), _d(code, reason, crashed=crashed, stop_req=stop_req, neg=neg)


def test_sweep_invariants():
    for (code, reason, crashed, stop_req, neg), (status, err, by) in _sweep():
        case = f"code={code} reason={reason} crashed={crashed} stop_req={stop_req} neg={neg} -> {status, err, by}"
        assert status in _STATUSES, case
        # Completed means the run finished on its own: dependents get released.
        if status == STATUS_COMPLETED:
            assert code == 0 or neg, case
            assert err in (None, tqerr.RUN_NEGATIVE), case
            assert reason not in FORCED_STOP_REASONS and reason != "deps_invalidated", case
            assert not crashed, case  # a crashed reader never saw the run finish
        if reason in PRESSURE_STOP_REASONS:
            assert status == STATUS_STOPPED and err in (tqerr.STOP_RAM, tqerr.STOP_VRAM), case
        if status == STATUS_FAILED:
            assert err == tqerr.RUN_NONZERO, case
        # Attribution only on stop outcomes, and "tq" only for tq-initiated reasons.
        assert by in (None, "tq", "host-sleep", "user"), case
        if by is not None:
            assert status in (STATUS_INTERRUPTED, STATUS_STOPPED), case
        if by == "tq":
            assert reason in TQ_STOP_REASONS, case
