"""Behavior tests for static/js/views/*.js (run through Node with app.js globals stubbed)."""
from __future__ import annotations

import re
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "taskqueue" / "static" / "js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

STUBS = r"""
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null };
globalThis.i18n = { t: k => k };
globalThis.tqAct = c => JSON.stringify(c);
globalThis.TQ_THIS = { $: 'this' };
globalThis.badge = s => '[' + s + ']';
globalThis.tqProjectQuery = () => '';
globalThis.showToast = () => {};
globalThis.toastResult = (req, ok, after) => Promise.resolve(req).then(d => { showToast(ok, 'ok'); if (after) after(d); return d; }, e => { showToast(String(e.message || e), 'err'); return false; });
globalThis.empties = [];
globalThis._showPageEmpty = (e, w, show) => empties.push([e, w, show]);
globalThis._csvCell = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
globalThis.els = {};
globalThis.document = { getElementById: id => els[id] || null, querySelectorAll: () => [] };
"""


def _run(code: str):
    r = subprocess.run([NODE, "--input-type=module", "-e", STUBS + code], capture_output=True, text=True,
                       encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _imp(rel: str) -> str:
    return json.dumps((JS / rel).as_uri())


def test_audit_view_renders_rows_and_config_diff_with_rollback():
    out = _run(rf"""
    const rows = [{{ id: 1, ts: '', actor: '<a>', action: 'config', project: 'p', target: 't',
                     detail: JSON.stringify({{ changes: {{ k: {{ old: 1, new: 2 }} }} }}) }},
                  {{ id: 2, ts: '', actor: 'u', action: 'x', detail: 'plain <b>' }}];
    globalThis.fetch = async u => ({{ ok: true, json: async () => ({{ events: rows }}) }});
    els['audit-tbody'] = {{ innerHTML: '' }};
    const m = await import({_imp("views/audit.js")});
    const {{ S }} = await import({_imp("core/state.js")});
    await m.loadAudit();
    console.log(JSON.stringify({{ html: els['audit-tbody'].innerHTML, n: S.auditRows.length, empties }}));""")
    assert out["n"] == 2 and out["empties"][-1][2] is False
    assert "&lt;a&gt;" in out["html"] and "plain &lt;b&gt;" in out["html"]
    assert "<code>k</code>: 1 → 2" in out["html"] and "tqRollbackConfig" in out["html"]
    assert 'class="proj-tag"' in out["html"] and ">p</span>" in out["html"]


def test_audit_export_builds_csv_from_state():
    out = _run(rf"""
    let blobText = null;
    globalThis.Blob = class {{ constructor(p) {{ blobText = p.join(''); }} }};
    globalThis.URL = {{ createObjectURL: () => 'blob:x', revokeObjectURL() {{}} }};
    let clicked = 0;
    document.createElement = () => ({{ click() {{ clicked++; }} }});
    const m = await import({_imp("views/audit.js")});
    const {{ S }} = await import({_imp("core/state.js")});
    S.auditRows = [{{ ts: 't', actor: 'a', action: 'x', project: 'p', target: 'g', detail: 'd"q' }}];
    m.tqExportAudit();
    console.log(JSON.stringify({{ blobText, clicked }}));""")
    assert out["clicked"] == 1
    assert out["blobText"].split("\n") == ["ts,actor,action,project,target,detail", '"t","a","x","p","g","d""q"']


def test_tokens_view_hides_revoke_for_revoked_and_revoke_calls_delete():
    out = _run(rf"""
    const rows = [{{ id: 5, name: 'n', prefix: 'tq_', scope: 'read', projects: ['a', 'b'], created_at: '' }},
                  {{ id: 6, name: 'r', prefix: 'tq_', scope: 'admin', revoked_at: '2020-01-01' }}];
    globalThis.fetch = async u => ({{ ok: true, json: async () => ({{ tokens: rows }}) }});
    els['tokens-tbody'] = {{ innerHTML: '' }};
    const calls = [];
    globalThis._sendJson = (m, u) => {{ calls.push([m, u]); return Promise.resolve({{}}); }};
    const m = await import({_imp("views/tokens.js")});
    await m.loadApiTokens();
    m.tqRevokeToken(5);
    await new Promise(r => setTimeout(r, 0));
    console.log(JSON.stringify({{ html: els['tokens-tbody'].innerHTML, calls }}));""")
    assert out["html"].count("tqRevokeToken") == 1 and "read · a, b" in out["html"]
    assert out["calls"][0] == ["DELETE", "/api/tokens/5"]


def test_schedule_view_selection_state_and_pause_url():
    out = _run(rf"""
    const rows = [{{ project: 'p', task_id: 't1', cron: '* * * * *', status: 'queued', enabled: false, next_fire: null }}];
    globalThis.fetch = async u => ({{ ok: true, json: async () => ({{ schedules: rows }}) }});
    els['sched-tbody'] = {{ innerHTML: '' }};
    const posts = [];
    globalThis._postJson = u => {{ posts.push(u); return Promise.resolve({{}}); }};
    const opened = [];
    globalThis.openYamlModal = (p, t) => opened.push([p, t]);
    const m = await import({_imp("views/schedule.js")});
    const {{ S }} = await import({_imp("core/state.js")});
    m.tqSelectSchedule({{ getAttribute: () => JSON.stringify(['p', 't1']) }});
    m.tqNewSchedule();
    m.tqSchedPause('p', 't 1');
    await m.loadSchedules();
    console.log(JSON.stringify({{ key: S.schedKey, opened, posts, html: els['sched-tbody'].innerHTML }}));""")
    assert out["key"] == '["p","t1"]'
    assert out["opened"] == [["p", "t1"]]
    assert out["posts"] == ["/api/enable/p/t%201?enabled=0"]
    assert 'class="qrow-kbd"' in out["html"] and "paused" in out["html"]
    assert "tqSchedResume" in out["html"] and "tqSchedPause" not in out["html"]  # paused row offers Resume
    assert 'class="proj-tag"' in out["html"] and ">p</span>" in out["html"]


def test_alerts_view_orders_rows_and_patch_sends_flag():
    out = _run(rf"""
    globalThis.fetch = async u => ({{ ok: true, json: async () => u.includes('/system')
      ? {{ alerts: [{{ key: 'all_done', enabled: 1, firing: true, fire_count: 2 }}, {{ key: 'any_failure', enabled: 0 }}, {{ key: 'bogus' }}] }}
      : {{ events: [{{ ts: '', status: 'alert:any_failure', description: 'l1\nl2', detail: {{}} }}] }} }});
    globalThis.tqAlertDetailHtml = () => 'DETAIL';
    els['alerts-tbody'] = {{ innerHTML: '' }};
    els['alerts-hist-tbody'] = {{ innerHTML: '' }};
    const sent = [];
    globalThis._sendJson = (m, u, b) => {{ sent.push([m, u, b]); return Promise.resolve({{}}); }};
    globalThis.refresh = () => {{}};
    const m = await import({_imp("views/alerts.js")});
    const {{ S }} = await import({_imp("core/state.js")});
    await m.loadAlerts();
    m.tqPatchSystemAlert('all_done', 'enabled', {{ checked: true }});
    m.tqPatchSystemAlert('all_done', 'windows_toast', false);
    console.log(JSON.stringify({{ rows: els['alerts-tbody'].innerHTML, hist: els['alerts-hist-tbody'].innerHTML, sent, n: S.systemAlerts.length }}));""")
    r = out["rows"]
    assert r.index("alerts.sys.any_failure") < r.index("alerts.sys.all_done") and "bogus" not in r
    assert "DETAIL" in out["hist"] and out["n"] == 3
    assert out["sent"] == [["PATCH", "/api/alerts/system/all_done", {"enabled": 1}],
                           ["PATCH", "/api/alerts/system/all_done", {"windows_toast": 0}]]


def test_usage_view_range_type_toggle_and_summary():
    out = _run(rf"""
    const ctx = new Proxy({{}}, {{ get: (t, k) => (k in t ? t[k] : () => {{}}), set: (t, k, v) => (t[k] = v, true) }});
    const canvas = {{ style: {{}}, offsetWidth: 600, getContext: () => ctx, getBoundingClientRect: () => ({{ left: 0 }}) }};
    const btns = {{}};
    for (const id of ['usage-range-1d', 'usage-range-7d', 'usage-range-30d', 'usage-type-line', 'usage-type-area', 'usage-type-bar'])
      btns[id] = {{ style: {{}} }};
    Object.assign(els, btns, {{ 'usage-canvas': canvas, 'usage-empty': {{ style: {{}} }}, 'usage-legend': {{ innerHTML: '' }},
      'usage-summary-table': {{ innerHTML: '' }}, 'usage-project-select': null }});
    const urls = [];
    const pts = [{{ ts: 1, gpu_hours: 1.5, cpu_hours: 2 }}, {{ ts: 2, gpu_hours: 0.5, cpu_hours: 1 }}];
    globalThis.fetch = async u => (urls.push(u), {{ ok: true, json: async () => ({{ by_project: {{ 'p<x>': pts }},
      gpu_today: {{ 'p<x>': {{ used_hours: 1, limit_hours: 4, remaining_hours: 3 }} }} }}) }});
    const m = await import({_imp("views/usage.js")});
    const {{ S }} = await import({_imp("core/state.js")});
    m.setUsageRange(30);
    await new Promise(r => setTimeout(r, 20));
    m.setUsageType('bar');
    m.tqToggleUsageSeries('p<x>:gpu');
    const off = S.usageToggle['p<x>:gpu'];
    await new Promise(r => setTimeout(r, 20));
    console.log(JSON.stringify({{ urls, w30: btns['usage-range-30d'].style.fontWeight, w7: btns['usage-range-7d'].style.fontWeight,
      type: S.usageChartType, barW: btns['usage-type-bar'].style.fontWeight, off,
      sum: els['usage-summary-table'].innerHTML, legend: els['usage-legend'].innerHTML, chart: !!S.usageChart }}));""")
    assert out["urls"][0] == "/api/usage?days=30" and len(out["urls"]) == 2
    assert out["w30"] == "700" and out["w7"] == "400" and out["type"] == "bar" and out["barW"] == "700"
    assert out["off"] is False and out["chart"]
    assert "p&lt;x&gt;" in out["sum"] and "2.00" in out["sum"] and "1.00 / 4" in out["sum"]
    assert "tqToggleUsageSeries" in out["legend"]


_HISTORY_STUBS = r"""
globalThis._updateSortTh = (...a) => (globalThis.sortCalls ||= []).push(a);
globalThis._taskCopyIconBtn = () => '';
globalThis._isRestartStatus = s => s === 'interrupted';
globalThis._requeueLabel = s => 'requeue-' + s;
globalThis._ansiToHtml = s => s;
globalThis.refresh = () => { globalThis.refreshed = (globalThis.refreshed || 0) + 1; };
globalThis.fetched = [];
globalThis._fetchLog = async (id, pre) => { fetched.push([id, pre]); };
"""

_HISTORY_ROWS = r"""
const rows = [
  { run_id: 1, project: 'a', task_id: 't1', status: 'completed', started_at: '2024-01-01T00:00:00Z', finished_at: '2024-01-01T00:01:05Z', exit_code: 0, worker_id: '' },
  { run_id: 3, project: 'b', task_id: '<t3>', status: 'failed', started_at: '2024-01-02T00:00:00Z', finished_at: '2024-01-02T00:00:10Z', exit_code: 1, worker_id: 'w1',
    error_category: 'oom', anomalies: [{ metric: 'ram', value: 9, baseline_p95: 3, ratio: 3 }] },
  { run_id: 2, project: 'a', task_id: 't2', status: 'interrupted', started_at: '', worker_id: 'w1' },
];
"""


def _history_setup(extra: str = "") -> str:
    return _HISTORY_STUBS + _HISTORY_ROWS + rf"""
    els['history-tbody'] = {{ innerHTML: '' }};
    els['history-mobile-cards'] = {{ innerHTML: '' }};
    els['history-pagination'] = {{ innerHTML: '', textContent: '' }};
    const m = await import({_imp("views/history.js")});
    const {{ S }} = await import({_imp("core/state.js")});
    S.history = rows; S.historyTotal = rows.length;
    {extra}"""


def test_history_view_sorts_filters_and_escapes():
    out = _run(_history_setup(r"""
    m.renderHistory();
    const byRun = els['history-tbody'].innerHTML;
    S.historySort = { field: 'run_id', dir: 1 };
    m.renderHistory();
    const asc = els['history-tbody'].innerHTML;
    S.historyStatusFilter = 'failed';
    m.renderHistory();
    const failedOnly = els['history-tbody'].innerHTML;
    S.historyStatusFilter = 'all'; S.globalWorkerFilter = 'master';
    m.renderHistory();
    const masterOnly = els['history-tbody'].innerHTML;
    const desktopCards = els['history-mobile-cards'].innerHTML;
    globalThis.matchMedia = () => ({ matches: true }); globalThis.window.matchMedia = globalThis.matchMedia;
    m.renderHistory();
    const mobileRows = els['history-tbody'].innerHTML;
    console.log(JSON.stringify({ byRun, asc, failedOnly, masterOnly, cards: els['history-mobile-cards'].innerHTML, desktopCards, mobileRows,
      prev: S.prevHistoryStr.length > 10, sortCalls,
      anom: m._anomalyBadge(rows[1]), noAnom: m._anomalyBadge(rows[0]) }));"""))
    def ids(h):
        return [int(x) for x in __import__("re").findall(r'<td class="mob-hide mono">#(\d+)</td>', h)]
    assert ids(out["byRun"]) == [3, 2, 1] and ids(out["asc"]) == [1, 2, 3]
    assert ids(out["failedOnly"]) == [3] and ids(out["masterOnly"]) == [1]
    assert re.search(r'<span class="proj-tag"[^>]*>b/</span>&lt;t3&gt;', out["byRun"])
    assert "requeue-failed" in out["byRun"] and "requeue-interrupted" in out["byRun"]
    # F-110: renderHistory must not set prevHistoryStr (refresh.js owns identity).
    assert "1m05s" in out["byRun"] and out["cards"].count('class="run-card"') == 1 and not out["prev"]
    assert out["desktopCards"] == "" and out["mobileRows"] == ""  # only the visible layout is built
    assert "anomaly-badge" in out["anom"] and out["noAnom"] == ""


def test_history_view_pagination_and_filter_actions():
    out = _run(_history_setup(r"""
    S.historyTotal = 50;
    m.renderHistory();
    const more = els['history-pagination'].innerHTML;
    m.loadMoreHistory();
    const limit = S.historyLimit;
    S.historyTotal = 3;
    m.renderHistory();
    const done = els['history-pagination'].textContent;
    const cls = [];
    const mk = () => ({ classList: { add: c => cls.push(c) } });
    document.querySelectorAll = () => [{ classList: { remove: () => cls.push('rm') } }];
    m.setHistoryFilter('failed', mk());
    console.log(JSON.stringify({ more, limit, done, refreshed, filter: S.historyStatusFilter, cls }));"""))
    assert "loadMoreHistory" in out["more"] and "3 / 50" in out["more"]
    assert out["limit"] == 40 and out["refreshed"] == 1 and out["done"] == "3 runs"
    assert out["filter"] == "failed" and out["cls"] == ["rm", "active"]


def test_history_log_toggles_track_expanded_state_in_s():
    out = _run(_history_setup(r"""
    const mkEl = () => ({ classList: { add() {}, remove() {} }, style: {}, scrollIntoView() {}, textContent: '' });
    els['h-log-row-9'] = mkEl(); els['h-logbtn-9'] = mkEl(); els['hc-log-wrap-9'] = mkEl(); els['hc-logbtn-9'] = mkEl();
    const lp = await import(""" + _imp("components/logpanel.js") + r""");
    await lp.toggleHistoryLog(9, 'h-log-row-9', true);
    const openA = JSON.stringify(S.expandedLogs);
    await lp.toggleHistoryLog(9, 'h-log-row-9', true);
    const closedA = JSON.stringify(S.expandedLogs);
    await lp.toggleHistoryCardLog(9, false);
    const wrapOpen = els['hc-log-wrap-9'].style.display;
    await lp.toggleHistoryCardLog(9, false);
    console.log(JSON.stringify({ openA, closedA, wrapOpen, wrapClosed: els['hc-log-wrap-9'].style.display, btn: els['hc-logbtn-9'].textContent, fetched }));"""))
    assert out["openA"] == '{"h:9":{"content":"","isRunning":true}}' and out["closedA"] == "{}"
    assert out["wrapOpen"] == "block" and out["wrapClosed"] == "none" and out["btn"] == "btn.log"
    assert out["fetched"] == [[9, "h-log-pre-9"], [9, "hc-log-pre-9"]]


def test_history_run_history_panel_toggle_caches_html():
    out = _run(_history_setup(r"""
    globalThis._loadTaskRunHistoryHtml = async (p, t) => '<b>' + p + '/' + t + '</b>';
    const mk = () => ({ classList: { add() {}, remove() {} }, style: {}, innerHTML: '', textContent: '' });
    els['hc-hist-wrap-a-t1'] = mk(); els['hc-histbtn-a-t1'] = mk(); els['hc-hist-content-a-t1'] = mk();
    await m.toggleHistoryCardHist('a', 't1');
    const opened = [JSON.stringify(S.expandedHistory), els['hc-hist-content-a-t1'].innerHTML, els['hc-hist-wrap-a-t1'].style.display];
    await m.toggleHistoryCardHist('a', 't1');
    console.log(JSON.stringify({ opened, closed: JSON.stringify(S.expandedHistory), disp: els['hc-hist-wrap-a-t1'].style.display }));"""))
    assert out["opened"] == ['{"a/t1":"<b>a/t1</b>"}', "<b>a/t1</b>", "block"]
    assert out["closed"] == "{}" and out["disp"] == "none"


_QUEUE_STUBS = r"""
globalThis._updateSortTh = (...a) => (globalThis.sortCalls ||= []).push(a);
globalThis.refresh = () => { globalThis.refreshed = (globalThis.refreshed || 0) + 1; };
globalThis._syncUrl = o => (globalThis.urlSyncs ||= []).push(o);
globalThis._pageSize = () => 50;
globalThis.tqSyncQueueChrome = () => {};
globalThis.tqSyncSelectAll = () => {};
globalThis.tqSyncDrawerLive = () => {};
document.querySelector = () => null;
globalThis.mkEl = () => ({ innerHTML: '', value: '', hidden: false, style: {}, textContent: '',
  classList: { s: new Set(), add(c) { this.s.add(c); }, remove(c) { this.s.delete(c); }, has(c) { return this.s.has(c); } },
  scrollIntoView() {}, focus() {} });
"""


def test_queue_view_search_sort_and_pure_helpers():
    out = _run(_QUEUE_STUBS + rf"""
    for (const id of ['queue-search', 'queue-search-clear']) els[id] = mkEl();
    const m = await import({_imp("views/queue.js")});
    const {{ S }} = await import({_imp("core/state.js")});
    S.queuePage = 3;
    m.onQueueSearchInput();
    const searching = els['queue-search'].classList.has('tq-searching');
    els['queue-search'].value = 'abc';
    m.onQueueSearchInput();
    await new Promise(r => setTimeout(r, 450));
    const afterDebounce = {{ refreshed, urlSyncs: [...urlSyncs], page: S.queuePage, searching,
      still: els['queue-search'].classList.has('tq-searching'), hidden: els['queue-search-clear'].hidden }};
    m.clearQueueSearch();
    const cleared = {{ v: els['queue-search'].value, refreshed, hidden: els['queue-search-clear'].hidden }};
    S.queueSort = {{ field: 'status', dir: 1 }};
    m.sortQueue('status'); const flipped = S.queueSort.dir;
    m.sortQueue('name'); const swapped = {{ ...S.queueSort }};
    console.log(JSON.stringify({{ afterDebounce, cleared, flipped, swapped,
      key: m._queueSelKey('p', 't') === 'p\0t',
      pct: [m._progressPct({{ progress_pct: 40, status: 'running' }}), m._progressPct({{ progress_pct: 40, status: 'completed' }}),
            m._progressPct({{ progress_pct: 100, status: 'queued' }}), m._progressPct({{ progress_pct: 'x', status: 'failed' }}),
            m._progressPct({{ progress_pct: 55, status: 'failed' }}), m._progressPct(null)],
      bar: m._lastRunProgressHtml({{ status: 'failed<' }}, 33.3) }}));""")
    d = out["afterDebounce"]
    assert d["searching"] and d["refreshed"] == 1 and d["urlSyncs"] == [{"replace": True}] and d["page"] == 0
    assert d["still"] is False and d["hidden"] is False
    assert out["cleared"] == {"v": "", "refreshed": 2, "hidden": True}
    assert out["flipped"] == -1 and out["swapped"] == {"field": "name", "dir": 1} and out["key"]
    assert out["pct"] == [40, None, None, None, 55, None]
    assert "pbar-failed&lt;" in out["bar"] and "33% " in out["bar"]


def test_queue_view_render_empty_state_filters_and_flash():
    out = _run(_QUEUE_STUBS + rf"""
    for (const id of ['queue-all-tasks-heading', 'queue-today-note', 'queue-empty', 'queue-tbody', 'queue-mobile-cards',
                      'queue-pagination']) els[id] = mkEl();
    const m = await import({_imp("views/queue.js")});
    const {{ S }} = await import({_imp("core/state.js")});
    S.queue = [{{ project: 'a', task_id: 't1', status: 'completed', finished_at: '' }},
               {{ project: 'a', task_id: 't2', status: 'failed', last_worker_id: 'w1', finished_at: '' }}];
    S.queueTotal = 2; S.status = {{ running: [] }}; S.globalWorkerFilter = 'w9';
    m.renderQueue();
    const empty = {{ head: els['queue-all-tasks-heading'].textContent, emptyShown: els['queue-empty'].hidden === false,
      rows: S.queueExportRows.length, sort: sortCalls[0] }};
    S.globalWorkerFilter = 'w1';
    try {{ m.renderQueue(); }} catch (e) {{ /* row builders need many app.js globals; the filter ran before them */ }}
    const w1 = S.queueExportRows.map(t => t.task_id);
    // flash: sets key, auto-clears, reapply marks a freshly rendered row
    const row = mkEl(); els['qrow-1-a-t2'] = row;
    m._flashQueueRow('a', 't2');
    const flashed = {{ key: S.queueFlashKey === 'a\0t2', cls: row.classList.has('qrow-flash'), timer: !!S.queueFlashTimer }};
    row.classList.remove('qrow-flash');
    m._reapplyQueueFlash();
    const reapplied = row.classList.has('qrow-flash');
    clearTimeout(S.queueFlashTimer);
    console.log(JSON.stringify({{ empty, w1, flashed, reapplied, cols: m.QUEUE_COLS }}));""")
    assert out["empty"]["head"] == "All tasks (0 of 2)" and out["empty"]["emptyShown"] and out["empty"]["rows"] == 0
    assert out["empty"]["sort"] == ["q", "status", 1]
    assert out["w1"] == ["t2"]
    assert out["flashed"] == {"key": True, "cls": True, "timer": True} and out["reapplied"] and out["cols"] == 9


def test_dashboard_view_stats_cache_history_toggle_and_workers_table():
    out = _run(rf"""
    globalThis.i18n = {{ t: k => k }};
    globalThis.calls = [];
    globalThis._fetchJson = async u => {{
      calls.push(u);
      if (u.includes('bad')) throw new Error('boom');
      return {{ tasks: [{{ project: 'a', task_id: 't1', mean_sec: 120 }}, {{ project: 'a', task_id: 't2', p50_sec: 30 }}] }};
    }};
    globalThis._loadTaskRunHistoryHtml = async (p, t) => '<b>' + p + '/' + t + '</b>';
    globalThis.workerSync = () => {{}};
    const mkEl = () => ({{ innerHTML: '', textContent: '', style: {{}} }});
    for (const id of ['d-hist-wrap-a-t1', 'd-histbtn-a-t1', 'd-hist-content-a-t1', 'workers-tbody']) els[id] = mkEl();
    const m = await import({_imp("views/dashboard.js")});
    const {{ S }} = await import({_imp("core/state.js")});
    const running = [{{ project: 'a', task_id: 't1' }}, {{ project: 'a', task_id: 't2' }}, {{ project: 'a', task_id: 't3' }},
                     {{ project: 'bad', task_id: 'x' }}];
    await m._ensureDashStats(running);
    await m._ensureDashStats(running);   // cached: no new fetches
    const stats = {{ n: calls.length, e1: m._dashExpectedSec('a', 't1'), e2: m._dashExpectedSec('a', 't2'),
      e3: m._dashExpectedSec('a', 't3'), ex: m._dashExpectedSec('bad', 'x'), nullCached: S.dashStatsByKey['bad/x'] === null }};
    await m.toggleDashHist('a', 't1');
    const open = {{ html: els['d-hist-content-a-t1'].innerHTML, disp: els['d-hist-wrap-a-t1'].style.display,
      btn: els['d-histbtn-a-t1'].textContent, key: Object.keys(S.expandedHistory) }};
    await m.toggleDashHist('a', 't1');
    const closed = {{ disp: els['d-hist-wrap-a-t1'].style.display, left: Object.keys(S.expandedHistory).length }};
    S.workers = []; S.status = {{ running: [{{ worker_id: 'w<1>', resources: {{ cpu_cores: 2, ram_gib: 1.5 }} }}] }}; S.queue = [];
    m.renderWorkers();
    const none = els['workers-tbody'].innerHTML;
    S.workers = [{{ worker_id: 'w<1>', status: 'online', os: 'win', arch: 'x64', cpu_cores: 8, ram_gib: 16, gpu_vram_gib: 4,
      projects: '["a"]', tags: '[]', version: '1.0', version_ok: true }}];
    S.queue = [{{ project: 'a', status: 'queued' }}, {{ project: 'z', status: 'queued' }}];
    m.renderWorkers();
    console.log(JSON.stringify({{ stats, open, closed, none, table: els['workers-tbody'].innerHTML }}));""")
    s = out["stats"]
    assert s["n"] == 2 and s["e1"] == 120 and s["e2"] == 30 and s["e3"] is None and s["ex"] is None and s["nullCached"]
    assert out["open"] == {"html": "<b>a/t1</b>", "disp": "block", "btn": "btn.hideHistory", "key": ["a/t1"]}
    assert out["closed"] == {"disp": "none", "left": 0}
    assert "workers.noWorkers" in out["none"]
    t = out["table"]
    assert "w&lt;1&gt;" in t and "2/8" in t and "1.5/16.0 GB" in t and "1 run / 1 queued" in t and "workerSync" in t


def test_history_search_all_logs_renders_matches():
    out = _run(_history_setup(r"""
    globalThis.tqAct = a => JSON.stringify(a).replace(/"/g, '&quot;');
    els['history-search'] = { value: ' oom ' };
    els['history-logsearch'] = { innerHTML: '', textContent: '', hidden: true };
    els['history-search-clear'] = { hidden: true };
    const urls = [];
    globalThis.fetch = async u => (urls.push(u), { ok: true, json: async () => ({ q: 'oom', truncated: false,
      matches: [{ run_id: 7, project: 'a', task_id: 't<1>', line: 'CUDA <oom>' }] }) });
    await m.searchAllLogs();
    const html = els['history-logsearch'].innerHTML;
    const shown = !els['history-logsearch'].hidden;
    m.openLogSearchRun(7);
    console.log(JSON.stringify({ urls, html, shown, q: els['history-search'].value }));"""))
    assert out["urls"] == ["/api/log-search?q=oom"] and out["shown"]
    assert "t&lt;1&gt;" in out["html"] and "CUDA &lt;oom&gt;" in out["html"] and "openLogSearchRun" in out["html"]
    assert out["q"] == "#7"
