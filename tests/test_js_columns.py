"""Behavior tests for components/columns.js (Queue column chooser)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV


def test_hidden_columns_persist_and_generate_css():
    out = run_js(EV + """
const { g, doc } = await setup({ app: true, routes: {} });
await g.flush();
g.tqSetQueueColumn('description', false);
g.tqSetQueueColumn('priority', false);
g.tqSetQueueColumn('priority', true);
const css = g.tqQueueColumnsCss(g.tqHiddenQueueCols());
console.log('RESULT:' + JSON.stringify({ hidden: g.tqHiddenQueueCols(), css, stored: g.localStorage.getItem('tq_queue_hidden_cols') }));
""")
    assert out["hidden"] == ["description"]
    assert "nth-child(8)" in out["css"] and "nth-child(2)" not in out["css"]
    assert 'tr[id^="qrow-"]' in out["css"]  # log/history sub-rows are not touched
    assert out["stored"] == '["description"]'
