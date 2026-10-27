from taskqueue import errors as tqerr
from taskqueue.scheduler import _OccupancyState, _p6_annotate_hold, _resource_block


def test_p6_annotate_hold_names_the_reserved_task():
    out = _p6_annotate_hold("need 4.0 (hold 2.5)", ("p", "t"), 2.5, 0.0)
    assert out == "need 4.0 (hold 2.5 (reserved for p/t))"
    assert _p6_annotate_hold("no hold text", ("p", "t"), 2.5, 0.0) == "no hold text"
    assert _p6_annotate_hold("x (hold 0.0)", ("p", "t"), 0.0, 0.0) == "x (hold 0.0)"


def test_resource_block_vram_over_system_is_permanent():
    est = {"vram_gib": 40.0, "source": "decl"}
    code, reason = _resource_block("short", 4.0, 40.0, est, 24.0)
    assert code == tqerr.BLOCK_VRAM_EXCEEDS_SYSTEM
    assert reason == "short [est_ram=4.0 est_vram=40.0 src=decl]"
    code, reason = _resource_block("", 4.0, 8.0, {"vram_gib": 6.0, "source": "hist"}, 24.0)
    assert code == tqerr.BLOCK_RESOURCES
    assert reason.startswith("insufficient resources [est_ram=4.0 admit_vram=8.0 decl_vram=6.0")


def test_occupancy_note_admitted_updates_every_counter():
    occ = _OccupancyState.from_snapshot({
        "n_running": 1, "has_exclusive": False, "exclusive_task": "", "exclusive_projects": {},
        "n_running_by_proj": {"p": 1}, "cpu_by_proj": {}, "ram_by_proj": {}, "gpu_by_proj": {},
        "total_cpu_declared": 1.0, "total_ram_declared": 2.0, "running_locks": {"a"},
    })
    occ.note_admitted("p", "t2", cpu=2.0, ram=3.0, gpu=1.0, locks={"b"}, unknown=True,
                      exclusive=False, exclusive_project=True)
    assert occ.n_running == 2 and occ.n_running_by_proj == {"p": 2}
    assert occ.cpu_by_proj == {"p": 2.0} and occ.ram_by_proj == {"p": 3.0} and occ.gpu_by_proj == {"p": 1.0}
    assert occ.total_cpu_declared == 3.0 and occ.total_ram_declared == 5.0
    assert occ.running_locks == {"a", "b"} and occ.n_unknown_running == 1
    assert occ.exclusive_projects == {"p": "p/t2"} and not occ.has_exclusive
    occ.note_admitted("p", "t3", cpu=1, ram=0, gpu=0, locks=set(), unknown=False,
                      exclusive=True, exclusive_project=False)
    assert occ.has_exclusive
