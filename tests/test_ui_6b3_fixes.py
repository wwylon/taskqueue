"""6b.3 frontend guards, as Node behavior tests (real app.js + modules under tests/js_dom_stub.mjs)."""

from __future__ import annotations

from tests.test_js_app import run_js


def test_soft_fetch_failure_keeps_last_good_rows() -> None:
    # _fetchJson itself (null on failure) is behavior-tested in test_js_modules.py; here each loader must keep its rows.
    out = run_js(
        """
const { g, doc } = await setup({ app: true });   // every /api/* route answers 404 -> fetchJson returns null
await g.flush();
for (const id of ['audit-tbody', 'tokens-tbody', 'sched-tbody', 'tasks-tbody']) doc.getElementById(id).innerHTML = '<tr><td>KEEP</td></tr>';
g.tqState.taskStatsRows = [{ project: 'p', task_id: 't' }];
g.tqState.auditRows = [{ action: 'x' }];
await g.loadAudit(); await g.loadApiTokens(); await g.loadSchedules(); await g.loadTaskStats(); await g.flush();
const r = { html: {}, stats: g.tqState.taskStatsRows.length, audit: g.tqState.auditRows.length };
for (const id of ['audit-tbody', 'tokens-tbody', 'sched-tbody', 'tasks-tbody']) r.html[id] = doc.getElementById(id).innerHTML;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert all(v == "<tr><td>KEEP</td></tr>" for v in out["html"].values()), out["html"]
    assert out["stats"] == 1 and out["audit"] == 1


def test_log_sse_is_capped_and_closed_on_pagehide() -> None:
    out = run_js(
        """
const { g } = await setup({ app: true });
await g.flush();
const map = g.tqState.logSseMap;
const hs = [1, 2, 3, 4, 5].map(i => { const h = g.tqEventSource('/api/logs/r' + i + '/stream'); g._trackLogSse(map, 'r' + i, h); return h; });
const r = { open: Object.keys(map).length, evicted: hs.filter(h => h._closed).length };
g.dispatchWinEvent({ type: 'pagehide' });
r.afterHide = Object.keys(map).length; r.allClosed = hs.every(h => h._closed);
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["open"] == 3 and out["evicted"] == 2
    assert out["afterHide"] == 0 and out["allClosed"] is True


def test_csv_cells_neutralise_formulas() -> None:
    out = run_js(
        """
const { g, S } = await setup({ app: true });
await g.flush();
const r = { cells: ['=1+1', '+x', '-x', '@a', '-5', '3.5', 'a,b', 'q"q', null].map(g._csvCell) };
// the audit export goes through the same guard
let text = '';
g.Blob = class { constructor(parts) { text = parts.join(''); } };
g.URL = { createObjectURL: () => 'blob:x', revokeObjectURL() {} };
S.auditRows = [{ ts: 't', actor: '=cmd|x', action: 'a', project: 'p', target: '', detail: '' }];
g.tqExportAudit();
r.export = text.split('\\n')[1];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["cells"] == ["'=1+1", "'+x", "'-x", "'@a", "-5", "3.5", '"a,b"', '"q""q"', ""]
    assert out["export"].split(",")[1] == "'=cmd|x"
