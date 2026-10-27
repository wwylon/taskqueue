"""RAM soft/hard gates, antiflap, and capacity seeding."""

from __future__ import annotations

from taskqueue.resources import ResourceBudget


def _budget(*, used: float, total: float = 64.0, soft: int = 80, hard: int = 95) -> ResourceBudget:
    system = {
        "cpu_total_cores": 8,
        "ram_total_gib": total,
        "gpu_vram_total_gib": 0.0,
        "gpus": [],
    }
    cfg = {"cpu_limit_pct": 100, "gpu_limit_pct": 100, "ram_limit_pct": soft, "ram_hard_limit_pct": hard}
    b = ResourceBudget(system, cfg)
    b.ram_live = used
    b.ram_reserved = used
    return b


def test_ram_gate_is_additive() -> None:
    """A task whose estimate would push the host over soft must be blocked.

    Superseded test_ram_hint_does_not_block_admit_under_soft, which asserted the
    opposite and encoded F1: fits() compared `ram_reserved > ram_limit` without
    adding the candidate's own estimate, so admission only stopped once the host
    was ALREADY over soft — overshooting by at least one task every time.
    """
    b = _budget(used=20.0, soft=80)  # soft=51.2
    ok, reason = b.fits(1, 0.0, 48.0)  # 20 + 48 = 68 > 51.2
    assert not ok
    assert "68.0" in reason and "51.2" in reason


def test_ram_gate_admits_when_estimate_fits() -> None:
    b = _budget(used=20.0, soft=80)  # soft=51.2
    ok, reason = b.fits(1, 0.0, 25.0)  # 20 + 25 = 45 < 51.2
    assert ok, reason


def test_ram_soft_blocks_when_host_over_soft() -> None:
    b = _budget(used=55.0, soft=80)  # soft=51.2
    ok, reason = b.fits(1, 0.0, 1.0)
    assert not ok
    assert "soft" in reason.lower()


def test_same_tick_reserve_raises_soft_pressure() -> None:
    """Reserves accumulate within a tick, so later candidates see the pressure."""
    b = _budget(used=20.0, soft=80)  # soft=51.2
    assert b.fits(1, 0.0, 25.0)[0]   # fits: 20 + 25 = 45
    b.reserve(1, 0.0, 25.0)          # now reserved = 45
    ok, _ = b.fits(1, 0.0, 10.0)     # 45 + 10 = 55 > 51.2
    assert not ok


def test_antiflap_blocks_when_peak_exceeds_hard_headroom() -> None:
    b = _budget(used=50.0, soft=80, hard=95)  # hard=60.8
    blocked, reason = b.antiflap_ram(8.0, 20.0)
    assert blocked
    assert "hard" in reason.lower()


def test_antiflap_allows_small_peak() -> None:
    b = _budget(used=20.0, soft=80, hard=95)
    blocked, _ = b.antiflap_ram(8.0, 4.0)
    assert not blocked


def test_antiflap_ignores_p6_headroom_hold() -> None:
    """P6 reservation inflates ram_reserved but must not block antiflap for other tasks."""
    b = _budget(used=45.0, total=64.0, soft=90, hard=95)  # hard ≈ 60.8
    b.reserve(0, 0.0, 28.0)  # simulate top-task headroom hold
    assert b.ram_reserved == 73.0
    blocked, reason = b.antiflap_ram(8.0, 0.0)  # live 45 + 8 < hard
    assert not blocked, reason
    blocked_top, reason_top = b.antiflap_ram(28.0, 0.0)  # live 45 + 28 > hard
    assert blocked_top, reason_top
    assert "live 45.0" in reason_top


def test_soft_gate_message_splits_live_and_hold() -> None:
    """Soft refusal must not imply physical used > installed RAM."""
    b = _budget(used=55.0, total=64.0, soft=90, hard=95)  # soft ≈ 57.6
    b.reserve(0, 0.0, 28.0)  # P6 hold → reserved 83
    ok, reason = b.fits(1, 0.0, 4.0)
    assert not ok
    assert "live 55.0" in reason
    assert "hold 28.0" in reason
    assert "used/reserved" not in reason


def test_fits_ram_hold_credit_excludes_p6_from_breakdown() -> None:
    """Evaluating the P6 hold task must not show hold + est for the same GiB."""
    b = _budget(used=32.3, total=63.5, soft=80, hard=95)  # soft ≈ 50.8
    b.reserve(0, 0.0, 26.0)  # P6 hold on top of live
    ok, reason = b.fits(1, 0.0, 26.0, ram_hold_credit=26.0)
    assert not ok
    assert "hold 26.0" not in reason
    assert "live 32.3" in reason
    assert "est 26.0" in reason
    assert "need 58.3" in reason


def test_p6_hold_skipped_when_peak_still_in_live() -> None:
    """Stopped task RSS still in live must not get a second P6 hold."""
    b = _budget(used=32.3, total=63.5, soft=80, hard=95)
    # Zombie: peak 26 still in live — no incremental hold reserved.
    ok, reason = b.fits(1, 0.0, 26.0)
    assert not ok
    assert "hold" not in reason
    assert "need 58.3" in reason
