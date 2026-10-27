from taskqueue.web.hardware import gpu_fit_summary

HW = {"ram_available_gib": 16, "gpu": [
    {"name": "A", "mem_total_gib": 24, "mem_used_gib": 20},
    {"name": "B", "mem_total_gib": 24, "mem_used_gib": 4},
    {"name": "C", "mem_total_gib": 12, "mem_used_gib": 2},
]}


def test_fits_picks_tightest_gpu():
    r = gpu_fit_summary(HW, 8)
    assert [g["fits"] for g in r["gpus"]] == [False, True, True]
    assert r["best_gpu"] == 2 and r["fits_now"]


def test_nothing_fits_or_ram_short():
    assert not gpu_fit_summary(HW, 30)["fits_now"]
    assert not gpu_fit_summary(HW, 1, ram_gib=64)["fits_now"]
    assert gpu_fit_summary(HW, 0)["fits_now"]
