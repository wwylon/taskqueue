import json
import sqlite3

from taskqueue.deprefs import resolve_dep_ref, row_dep_refs


def _row(**cols):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    names = ", ".join(f"? AS {k}" for k in cols)
    return conn.execute(f"SELECT {names}", tuple(cols.values())).fetchone()


def test_resolve_dep_ref_bare_and_cross_project():
    assert resolve_dep_ref("p", "a") == ("p", "a", "a")
    assert resolve_dep_ref("p", "q/a") == ("q", "a", "q/a")


def test_row_dep_refs_resolves_and_tolerates_bad_input():
    row = _row(depends_on=json.dumps(["a", "q/b"]), depends_on_any="not json")
    assert row_dep_refs(row, "depends_on", "p") == [("p", "a", "a"), ("q", "b", "q/b")]
    assert row_dep_refs(row, "depends_on_any", "p") == []
    assert row_dep_refs(row, "missing_col", "p") == []
    assert row_dep_refs(_row(depends_on=None), "depends_on", "p") == []
    assert row_dep_refs(_row(depends_on='{"x": 1}'), "depends_on", "p") == []
