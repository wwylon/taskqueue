from taskqueue.project_colors import assign_project_colors, delta_e, ensure_project_colors, hash_color, pick_color
from taskqueue.db import get_config, open_db


def test_deterministic_and_distinct():
    a = assign_project_colors({}, ["p1", "p2", "p3", "p4", "p5"])
    assert a == assign_project_colors({}, ["p5", "p4", "p3", "p2", "p1"])
    cols = list(a.values())
    assert all(c.startswith("#") and len(c) == 7 for c in cols)
    m = min(delta_e(x, y) for i, x in enumerate(cols) for y in cols[i + 1:])
    assert m > 15


def test_existing_kept_and_new_far_from_used():
    base = {"a": "#ff0000"}
    out = assign_project_colors(base, ["a", "b"])
    assert out["a"] == "#ff0000"
    assert delta_e(out["b"], "#ff0000") > 30
    assert pick_color(["#ff0000"], "x") == pick_color(["#ff0000"], "x")


def test_persisted_and_survives_reopen(tmp_path):
    db = open_db(tmp_path / "q.db")
    c1 = ensure_project_colors(db, ["alpha", "beta"])
    db.close()
    db = open_db(tmp_path / "q.db")
    assert get_config(db)["project_color_alpha"] == c1["alpha"]
    c2 = ensure_project_colors(db, ["alpha", "beta", "gamma"])
    assert c2["alpha"] == c1["alpha"] and c2["beta"] == c1["beta"] and "gamma" in c2


def test_hash_fallback_stable():
    assert hash_color("zz") == hash_color("zz")
