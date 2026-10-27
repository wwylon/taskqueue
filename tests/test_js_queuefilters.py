"""Behavior tests for components/queuefilters.js (saved filters, selection/bulk bar, retry-all, export)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: ROUTES });
await g.flush();
const toasts = []; g.showToast = (m, k) => toasts.push([m, k]);
const r = {};
"""


def _js(routes: str, body: str) -> str:
    return EV + PRE.replace("ROUTES", routes) + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_saved_filters_seed_persist_apply_save_and_delete():
    out = run_js(_js("{}", """
g.localStorage.removeItem('tq_saved_filters');
g.tqLoadSavedFilters();
r.seeds = S.savedFilters.map(f => f.id);
r.persisted = JSON.parse(g.localStorage.getItem('tq_saved_filters')).length;
g.localStorage.setItem('tq_saved_filters', JSON.stringify([{ id: 'x', name: 'Mine', status: 'failed', search: 'foo', project: 'all' }]));
g.tqLoadSavedFilters();
r.loaded = S.savedFilters.map(f => g.tqFilterLabel(f));
r.seedLabel = g.tqFilterLabel({ id: 'seed-ci' });
g.localStorage.setItem('tq_saved_filters', '{bad');
g.tqLoadSavedFilters();
r.fallback = S.savedFilters.length;
doc.add('saved-filters-pop'); doc.add('queue-search');
S.savedFilters = [{ id: 'f1', name: 'A<b>', status: 'running', search: 'ml', project: 'all', today: true }];
g.tqRenderSavedFilters();
const html = doc.getElementById('saved-filters-pop').innerHTML;
r.html = [html.includes('A&lt;b&gt;'), html.includes('tqApplySavedFilter'), html.includes('queue.failedTodayNote')];
S.savedFilters = [];
g.tqRenderSavedFilters();
r.empty = doc.getElementById('saved-filters-pop').innerHTML.includes('queue.savedEmpty');
S.savedFilters = [{ id: 'f1', name: 'A', status: 'running', search: 'ml', project: 'all', today: true }];
S.selectedProject = 'all'; S.applyingUrl = true; S.queuePage = 3;
let refreshed = 0; g.refresh = () => { refreshed++; };
g.tqApplySavedFilter('f1');
r.applied = [S.queueStatusFilter, S.savedToday, S.queuePage, doc.getElementById('queue-search').value, refreshed, g.localStorage.getItem('tq_status')];
g.tqApplySavedFilter('nope');
r.unknown = refreshed;
g.window.prompt = () => 'My filter';
S.queueStatusFilter = 'failed'; doc.getElementById('queue-search').value = ' zz ';
g.tqSaveCurrentFilter();
const saved = S.savedFilters[1];
r.saved = [saved.name, saved.status, saved.search, saved.today, toasts.pop()];
g.window.prompt = () => '   ';
g.tqSaveCurrentFilter();
r.blank = S.savedFilters.length;
const ev = { stopped: false, stopPropagation() { this.stopped = true; } };
g.tqDeleteSavedFilter('f1', ev);
r.deleted = [S.savedFilters.map(f => f.name), ev.stopped, JSON.parse(g.localStorage.getItem('tq_saved_filters')).length];
S.savedToday = true; S.queueStatusFilter = 'failed'; S.queuePage = 2;
g.tqClearQueueFilter();
r.cleared = [S.queueStatusFilter, S.savedToday, S.queuePage, refreshed];
"""))
    assert out["seeds"] == ["seed-failed-today", "seed-running", "seed-ci", "seed-ml"] and out["persisted"] == 4
    assert out["loaded"] == ["Mine"] and out["seedLabel"] == "queue.presetCi" and out["fallback"] == 4
    assert out["html"] == [True, True, True] and out["empty"] is True
    assert out["applied"] == ["running", True, 0, "ml", 1, "running"] and out["unknown"] == 1
    assert out["saved"] == ["My filter", "failed", "zz", True, ["queue.filterSaved", "ok"]]
    assert out["blank"] == 2
    assert out["deleted"] == [["My filter"], True, 1]
    assert out["cleared"] == ["all", False, 0, 2]


def test_selection_bulk_bar_and_chrome_sync():
    out = run_js(_js("{}", """
doc.add('queue-bulk-bar'); doc.add('q-select-all'); doc.add('retry-all-failed-btn', { tag: 'button' });
const rows = [{ project: 'p', task_id: 'a' }, { project: 'p', task_id: 'b' }];
S.queueExportRows = rows; S.queueSelection.clear();
const bar = () => doc.getElementById('queue-bulk-bar'), all = () => doc.getElementById('q-select-all');
g.tqSetRowSelected('p', 'a', true);
r.one = [bar().hidden, bar().innerHTML.includes('bulk-count">1<'), all().checked, all().indeterminate];
g.tqToggleSelectVisible(true);
r.all = [S.queueSelection.size, all().checked, all().indeterminate];
S.bulkBusy = true; g.tqRenderBulkBar();
r.busy = bar().innerHTML.includes(' disabled');
S.bulkBusy = false;
g.tqClearSelection();
r.cleared = [S.queueSelection.size, bar().hidden, bar().innerHTML, all().checked];
g.tqSyncSelectAll([]); r.emptyRows = all().checked;
S.queueStatusFilter = 'failed'; S.queueTotal = 7; S.savedToday = true;
g.tqSyncQueueChrome();
const btn = doc.getElementById('retry-all-failed-btn');
r.retryBtn = [btn.style.display, btn.textContent, btn.title];
S.queueStatusFilter = 'stopped'; S.savedToday = false; g.tqSyncQueueChrome();
r.restartBtn = [btn.style.display, btn.textContent, btn.title];
S.queueStatusFilter = 'all'; g.tqSyncQueueChrome();
r.hiddenBtn = btn.style.display;
"""))
    assert out["one"] == [False, True, False, True]
    assert out["all"] == [2, True, False] and out["busy"] is True
    assert out["cleared"] == [0, True, "", False] and out["emptyRows"] is False
    assert out["retryBtn"] == ["", "queue.retryAllFailed", "queue.retryAllQueryHint"]
    assert out["restartBtn"] == ["", "queue.restartAll", ""] and out["hiddenBtn"] == "none"


def test_bulk_action_counts_ok_failed_skipped_and_guards_reentry():
    out = run_js(_js("{}", """
doc.add('queue-bulk-bar');
S.queue = [{ project: 'p', task_id: 'a', status: 'running' }, { project: 'p', task_id: 'b', status: 'failed' }, { project: 'p', task_id: 'c', status: 'queued' }];
const calls = [];
const confirms = [];
g.openConfirm = (msg, onOk) => { confirms.push(msg); return onOk(); };
g._postJson = async (url, body) => {
  calls.push([url, body]);
  if (url === '/api/tasks/bulk') return { results: body.tasks.map(t => ({ task: t, ok: !t.endsWith('/c') })) };
  return { requeued: body.task_ids, skipped: [] };
};
let refreshed = 0; g.refresh = () => { refreshed++; };
for (const k of ['a', 'b', 'c']) S.queueSelection.add('p\\0' + k);
await g.tqBulkAction('stop');
r.stop = [calls.slice(), toasts.pop(), S.queueSelection.size, S.bulkBusy, refreshed, confirms[0]];
S.queueSelection.clear(); await g.tqBulkAction('retry'); r.none = calls.length;
for (const k of ['a', 'b']) S.queueSelection.add('p\\0' + k);
calls.length = 0;
await g.tqBulkAction('retry');
r.retry = [calls.slice(), toasts.pop(), S.queueSelection.size];
S.bulkBusy = true; S.queueSelection.add('p\\0a'); calls.length = 0;
await g.tqBulkAction('retry'); r.busy = calls.length;
"""))
    # one request for the stoppable rows (b is failed: skipped client-side)
    assert out["stop"][0] == [["/api/tasks/bulk", {"action": "stop", "tasks": ["p/a", "p/c"]}]]
    assert out["stop"][1] == ["queue.bulkDone", "err"]
    assert out["stop"][2:5] == [3, False, 1]
    assert out["stop"][5] == "queue.bulkStopConfirm"  # confirm before stop (count in i18n params)
    assert out["none"] == 1
    assert out["retry"] == [[["/api/tasks/retry-batch", {"task_ids": ["p/a", "p/b"]}]], ["queue.bulkDone", "ok"], 0]
    assert out["busy"] == 0


def test_select_all_matching_selects_every_filtered_task():
    out = run_js(_js("{}", """
doc.add('queue-bulk-bar');
S.queue = [{ project: 'p', task_id: 'a', status: 'failed' }];
S.queueTotal = 3; S.queueStatusFilter = 'failed';
let q = null;
g._fetchJson = async (url) => { q = url; return { tasks: [{ project: 'p', task_id: 'a', status: 'failed' }, { project: 'p', task_id: 'b', status: 'failed' }, { project: 'p', task_id: 'c', status: 'failed' }] }; };
S.queueSelection.add('p\\0a');
g.tqRenderBulkBar();
r.offer = doc.getElementById('queue-bulk-bar').innerHTML.includes('tqSelectAllMatching');
await g.tqSelectAllMatching();
r.size = S.queueSelection.size;
r.q = q;
"""))
    assert out["offer"] is True and out["size"] == 3
    assert "status=failed" in out["q"] and "limit=5000" in out["q"]


def test_retry_all_failed_queries_filters_confirms_over_500_and_reports():
    out = run_js(_js("{}", """
doc.add('queue-search').value = ' foo '; doc.add('retry-all-failed-btn', { tag: 'button' });
const retried = []; g._postJson = async (url, body) => { retried.push([url, body]); return { requeued: (body.task_ids || []).filter(t => !t.endsWith('/bad')), skipped: [] }; };
let q = null; g._fetchJson = async (url) => { q = url; return { tasks: [{ project: 'p', task_id: 'a' }, { project: 'p', task_id: 'bad' }] }; };
let refreshed = 0; g.refresh = () => { refreshed++; };
S.queueStatusFilter = 'all'; S.queueTotal = 3;
await g.tqRetryAllFailed(); r.wrongStatus = q;
S.queueStatusFilter = 'failed'; S.selectedProject = 'p'; S.tagFilter = 'gpu'; S.groupFilter = 'g1'; S.queueTotal = 9000;
let confirm = null; g.openConfirm = (m, cb, o) => { confirm = { m, cb, o }; };
await g.tqRetryAllFailed();
r.confirm = [confirm.m, confirm.o.confirmLabel, q];
await confirm.cb();
r.query = q.split('?')[1].split('&');
r.done = [retried.slice(), toasts.pop(), S.bulkBusy, refreshed, doc.getElementById('retry-all-failed-btn').disabled];
S.queueTotal = 2; S.queueStatusFilter = 'stopped'; confirm = null; retried.length = 0;
await g.tqRetryAllFailed();
await g.flush();
r.small = [confirm, retried.length, toasts.pop()];
g._fetchJson = async () => null;
S.queueStatusFilter = 'failed';
await g.tqRetryAllFailed(); await g.flush();
r.unreachable = toasts.pop();
"""))
    assert out["wrongStatus"] is None
    assert out["confirm"][0] == "queue.retryAllConfirm" and out["confirm"][2] is None or out["confirm"][2] is None
    assert out["query"] == ["status=failed", "limit=5000", "offset=0", "project=p", "search=foo", "tag=gpu", "group=g1"]
    assert out["done"] == [[["/api/tasks/retry-batch", {"task_ids": ["p/a", "p/bad"]}]], ["queue.retryAllDone", "err"], False, 1, False]
    assert out["small"][0] is None and out["small"][1] == 1 and out["small"][2] == ["queue.restartAllDone", "ok"] or out["small"][2] == ["queue.restartAllDone", "err"]
    assert out["unreachable"] == ["sse.error", "err"]


def test_csv_cell_escapes_formulas_and_export_builds_csv_and_json():
    out = run_js(_js("{}", """
r.cells = [g._csvCell(null), g._csvCell('a,b'), g._csvCell('q"x'), g._csvCell('=1+1'), g._csvCell('-5'), g._csvCell('-x'), g._csvCell('+SUM'), g._csvCell('@a'), g._csvCell('l\\nb')];
let blobs = [], names = [];
g.Blob = class { constructor(parts, o) { blobs.push([parts.join(''), o.type]); } };
g.URL = { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} };
const realCreate = doc.createElement.bind(doc);
doc.createElement = (t) => { const e = realCreate(t); if (t === 'a') e.click = () => names.push(e.download); return e; };
S.queueExportRows = [{ project: 'p', task_id: 't', status: 'failed', priority: 3, description: 'd,1', progress_pct: 0 }];
g.tqExportQueue('csv'); g.tqExportQueue('json');
r.csv = [blobs[0][1], blobs[0][0].split('\\n')];
const j = JSON.parse(blobs[1][0]);
r.json = [blobs[1][1], j[0].project, j[0].started_at, j[0].progress_pct, j[0].priority];
r.names = names;
"""))
    assert out["cells"] == ["", '"a,b"', '"q""x"', "'=1+1", "-5", "'-x", "'+SUM", "'@a", '"l\nb"']
    assert out["csv"][0] == "text/csv" and out["csv"][1][0].startswith("project,task_id,status")
    assert out["csv"][1][1] == 'p,t,failed,3,"d,1",,,,0'
    assert out["json"] == ["application/json", "p", None, 0, 3]
    assert out["names"] == ["queue.csv", "queue.json"]
