"""Characterization of the terminal-status decision for a reaped local run."""
from __future__ import annotations

import pytest

from taskqueue import errors as tqerr
from taskqueue.daemon import decide_terminal_status as d
from taskqueue.models import (
    STATUS_COMPLETED, STATUS_FAILED, STATUS_INTERRUPTED, STATUS_STOPPED, STATUS_SUSPENDED,
)

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
        ((0, "ram_hard"), {"crashed": True}, (STATUS_INTERRUPTED, None, "tq")),
        ((0, "wall_clock_cap"), {}, (STATUS_INTERRUPTED, tqerr.RUN_CAP, "tq")),
        ((1, "ram_hard"), {}, (STATUS_STOPPED, tqerr.STOP_RAM, "tq")),
        ((SUSP, "vram_hard"), {}, (STATUS_STOPPED, tqerr.STOP_VRAM, "tq")),
        ((SUSP,), {}, (STATUS_SUSPENDED, None, None)),
        ((SUSP,), {"stop_req": True}, (STATUS_SUSPENDED, None, None)),
        ((1, "frozen"), {}, (STATUS_INTERRUPTED, tqerr.RUN_FROZEN, "tq")),
        ((1, "wall_clock_cap"), {}, (STATUS_INTERRUPTED, tqerr.RUN_CAP, "tq")),
        ((1, "evicted"), {}, (STATUS_INTERRUPTED, None, "tq")),
        ((1,), {"stop_req": True}, (STATUS_INTERRUPTED, None, "user")),
        ((1, "host-sleep"), {"stop_req": True}, (STATUS_INTERRUPTED, None, "host-sleep")),
        ((3,), {"neg": True}, (STATUS_COMPLETED, tqerr.RUN_NEGATIVE, None)),
        ((2,), {}, (STATUS_FAILED, tqerr.RUN_NONZERO, None)),
    ],
)
def test_decide_terminal_status(args, kwargs, expected):
    assert _d(*args, **kwargs) == expected
