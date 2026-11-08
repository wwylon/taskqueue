"""Behavior tests for components/drawer.js (task drawer: info / YAML / history tabs and actions)."""

from __future__ import annotations

import json

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

TASK = {
    "project": "p",
    "task_id": "t1",
    "status": "failed",
    "priority": 40,
    "description": "train <x>",
    "enqueued_at": "2026-01-01T10:00:00Z",
    "run_id": 7,
}


def test_open_info_tab_paints_head_body_and_priority_actions():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true }});
await g.flush();
S.queue = [{TASK!r}];
const $ = id => doc.getElementById(id);
const r = {{}};
g.tqOpenDrawer('p', 't1');
r.back = !!$('tq-drawer-back').hidden;
r.head = [$('tq-drawer-id').textContent, $('tq-drawer-sub').textContent.includes('· ')];
r.retry = [$('tq-drawer-retry').disabled, $('tq-drawer-force').disabled, $('tq-drawer-stop').disabled];
r.body = $('tq-drawer-body').innerHTML;
r.activeTab = doc.querySelectorAll('.tq-drawer-tabs button').filter(b => b.classList.has('active')).map(b => b.getAttribute('data-dtab'));
const calls = [];
g.tqSetPriority = (p, t, n) => calls.push([p, t, n]);
g.tqDrawerPrio(-10);
$('tq-drawer-prio').value = '77';
g.tqDrawerPrio(0, true);
r.prio = calls;
const en = [];
g.tqSetEnabled = (p, t, on) => en.push(on);
g.tqDrawerToggleEnabled();
S.drawerTask.status = 'disabled'; g.tqDrawerToggleEnabled();
r.en = en;
g.tqCloseDrawer();
r.closed = [!!$('tq-drawer-back').hidden, S.drawerTask];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["head"][0] == "t1" and out["head"][1] is True
    assert out["retry"] == [False, True, True]  # failed: retry ok, no force/stop
    assert "train &lt;x&gt;" in out["body"] and "tqDrawerPrio" in out["body"] and 'value="40"' in out["body"]
    assert out["activeTab"] == ["info"]
    assert out["prio"] == [["p", "t1", 30], ["p", "t1", 77]]
    assert out["en"] == [0, 1]
    assert out["closed"][1] is None


def test_info_tab_lists_deps_while_queue_row_stays_compact():
    """Queue shows Deps: n/m + up to 3 unmet names; Details drawer lists every chip."""
    deps = [f"up{i}" for i in range(5)]
    task = {
        **TASK,
        "depends_on": deps,
        "dep_status": {d: "completed" for d in deps[:3]} | {d: "queued" for d in deps[3:]},
        "missing_deps": [],
    }
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true }});
await g.flush();
g.i18n = {{ t: (k, v) => {{
  if (k === 'task.depsCount') return 'Deps: ' + v.met + '/' + v.total;
  if (k === 'task.depsCountDead') return 'Deps: ' + v.ok + ' ok / ' + v.dead + ' broken of ' + v.total;
  if (k === 'task.depsMore') return 'more dependencies';
  if (k === 'drawer.deps') return 'Dependencies';
  return k;
}}}};
S.queue = [{task!r}];
const r = {{}};
r.queue = g._queueDepHtml(S.queue[0], {{}});
g.tqOpenDrawer('p', 't1');
r.body = doc.getElementById('tq-drawer-body').innerHTML;
r.chips = (r.body.match(/dep-tag/g) || []).length;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert "Deps: 3/5" in out["queue"]
    # F-182: unmet names (up3, up4) appear in the row; completed up0–up2 stay drawer-only.
    assert "up3" in out["queue"] and "up4" in out["queue"]
    assert "up0" not in out["queue"]
    assert out["chips"] == 5
    assert "Dependencies" in out["body"]
    assert "up0" in out["body"] and "up4" in out["body"]


def test_queue_dep_html_truncates_unmet_to_three_plus_ellipsis():
    """F-185: five unmet deps → three names + …; zero unmet → count only."""
    deps = [f"up{i}" for i in range(5)]
    waiting = {
        **TASK,
        "depends_on": deps,
        "dep_status": {d: "queued" for d in deps},
        "missing_deps": [],
    }
    done = {
        **TASK,
        "depends_on": deps,
        "dep_status": {d: "completed" for d in deps},
        "missing_deps": [],
    }
    out = run_js(
        EV
        + f"""
const {{ g }} = await setup({{ app: true }});
await g.flush();
g.i18n = {{ t: (k, v) => {{
  if (k === 'task.depsCount') return 'Deps: ' + v.met + '/' + v.total;
  if (k === 'task.depsMore') return 'more dependencies';
  return k;
}}}};
const r = {{}};
r.wait = g._queueDepHtml({waiting!r}, {{}});
r.done = g._queueDepHtml({done!r}, {{}});
r.waitTags = (r.wait.match(/dep-tag/g) || []).length;
r.hasEllipsis = r.wait.includes('…');
r.doneTags = (r.done.match(/dep-tag/g) || []).length;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["waitTags"] == 3
    assert out["hasEllipsis"] is True
    assert "Deps: 0/5" in out["wait"]
    assert out["doneTags"] == 0
    assert "Deps: 5/5" in out["done"]
    assert "…" not in out["done"]


def test_open_unknown_task_fetches_full_record_and_ignores_stale_response():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true, routes: { '/api/tasks/p/new': { task: { status: 'queued', priority: 5, description: 'fetched' } } } });
await g.flush();
const r = {};
g.tqOpenDrawer('p', 'new');
r.before = S.drawerTask.status || null;
await g.flush(); await g.flush();
r.after = [S.drawerTask.status, S.drawerTask.task_id, doc.getElementById('tq-drawer-body').innerHTML.includes('fetched')];
// the response for a task the user has since navigated away from is dropped
S.queue = [{ project: 'p', task_id: 'other', status: 'queued' }];
g.tqOpenDrawer('p', 'late'); g.tqOpenDrawer('p', 'other');
await g.flush(); await g.flush();
r.current = S.drawerTask.task_id;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["before"] is None
    assert out["after"] == ["queued", "new", True]
    assert out["current"] == "other"


def test_yaml_tab_loads_edits_saves_with_mtime_and_copies():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true }});
await g.flush();
S.queue = [{TASK!r}];
const reqs = []; let mtime = '5';
g.fetch = async (url, opts) => {{
  const o = opts || {{}};
  reqs.push({{ url, method: o.method || 'GET', mt: o.headers && o.headers['X-If-Mtime-Ns'], body: o.body }});
  if (o.method === 'PUT') return {{ ok: true, status: 200, json: async () => ({{ ok: true }}) }};
  return {{ ok: true, status: 200, headers: {{ get: () => mtime }}, text: async () => 'x: 1\\n' }};
}};
const $ = id => doc.getElementById(id);
const r = {{}};
g.tqOpenDrawer('p', 't1');
g.tqDrawerTab('yaml'); await g.flush(); await g.flush();
$('tq-yaml-editbox').style.display = 'none'; $('tq-yaml-save').style.display = 'none';   // stub does not parse inline style
r.loaded = [$('tq-yaml-pre').textContent, S.drawerYamlMtime];
g.tqDrawerToggleYaml();
r.editing = [$('tq-yaml-editbox').value, $('tq-yaml-editbox').style.display, $('tq-yaml-save').style.display];
$('tq-yaml-editbox').value = 'x: 2\\n'; mtime = '6';
await g.tqDrawerSaveYaml();
const put = reqs.find(q => q.method === 'PUT');
r.put = [put.mt, put.body];
r.saved = [$('tq-yaml-pre').textContent, $('tq-yaml-editbox').style.display, S.drawerYamlMtime];
const written = [];
g.navigator.clipboard.writeText = async t => {{ written.push(t); }};
await g.tqDrawerCopyYaml();
r.copied = written;
// server rejection keeps edit mode and shows the error
g.fetch = async () => ({{ ok: false, status: 409, json: async () => ({{ error: 'stale' }}) }});
g.tqDrawerToggleYaml();
await g.tqDrawerSaveYaml();
r.err = [$('tq-yaml-err').textContent, $('tq-yaml-err').style.display, $('tq-yaml-editbox').style.display];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["loaded"] == ["x: 1\n", "5"]
    assert out["editing"] == ["x: 1\n", "block", ""]
    assert out["put"] == ["5", "x: 2\n"]
    assert out["saved"] == ["x: 2\n", "none", "6"]
    assert out["copied"] == ["x: 2\n"]
    assert out["err"] == ["stale", "block", "block"]


def test_history_tab_lists_latest_five_runs_and_handles_empty():
    runs = [
        {
            "run_id": i,
            "status": "completed",
            "started_at": "2026-01-01T10:00:00Z",
            "finished_at": "2026-01-01T10:01:00Z",
        }
        for i in range(8)
    ]
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true, routes: {{ '/api/tasks/p/t1/runs': {{ runs: {runs!r} }}, '/api/tasks/p/none/runs': {{ runs: [] }} }} }});
await g.flush();
S.queue = [{TASK!r}, {{ project: 'p', task_id: 'none', status: 'queued' }}];
const body = () => doc.getElementById('tq-drawer-body');
const r = {{}};
g.tqOpenDrawer('p', 't1'); g.tqDrawerTab('history'); await g.flush(); await g.flush();
r.rows = body().querySelectorAll('.tq-hist-row').length;
r.first = body().innerHTML.includes('#0');
g.tqOpenDrawer('p', 'none'); g.tqDrawerTab('history'); await g.flush(); await g.flush();
r.empty = body().innerHTML.includes('drawer.noRuns');
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out == {"rows": 5, "first": True, "empty": True}


def test_drawer_actions_route_to_task_commands_and_log_fallbacks():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true }});
await g.flush();
S.queue = [{TASK!r}];
const calls = [];
g.retryTaskNew = (p, t, b) => calls.push(['retry', p, t, !!b]);
g.forceTask = (b, p, t) => calls.push(['force', p, t]);
g.stopTask = (b, p, t) => calls.push(['stop', p, t]);
g.tqOpenLogPanel = (...a) => calls.push(['panel', ...a]);
const r = {{}};
g.tqOpenDrawer('p', 't1');
g.tqDrawerAction('retry'); g.tqDrawerAction('force'); g.tqDrawerAction('stop');
// log: no matching inline row anywhere, so the bottom log panel opens and the drawer closes
g.tqDrawerAction('log');
r.calls = calls.slice();
r.closed = S.drawerTask === null;
// a task that never ran has no log: toast instead of a panel
S.queue = [{{ project: 'p', task_id: 'nolog', status: 'queued' }}];
calls.length = 0;
g.tqOpenDrawer('p', 'nolog'); g.tqDrawerAction('log');
r.noLog = [calls.length, (doc.body.children.find(c => c.id === 'tq-toast-host') || {{ children: [] }}).children.length];
// live sync: a status change in the caches repaints the head
S.queue = [{{ project: 'p', task_id: 'nolog', status: 'running', priority: 1 }}];
g.tqOpenDrawer('p', 'nolog'); S.queue[0].status = 'completed';
g.tqSyncDrawerLive();
r.synced = S.drawerTask.status;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["calls"] == [
        ["retry", "p", "t1", True],
        ["force", "p", "t1"],
        ["stop", "p", "t1"],
        ["panel", 7, "p", "t1", False],
    ]
    assert out["closed"] is True
    assert out["noLog"] == [0, 1]
    assert out["synced"] == "completed"


def test_why_button_shows_reasons_and_dry_run_verdict():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true, routes: {{
  '/why': () => ({{ ok: true, reasons: ['waiting on dependency p/up'], plan: {{ verdict: 'blocked', reason: 'waiting on p/up',
    need: {{ cpu_cores: 1, ram_gib: 20, gpu_vram_gib: 0 }}, free: {{ cpu_cores: 7, ram_gib: 10, gpu_vram_gib: 0 }} }} }}),
}} }});
await g.flush();
S.queue = [{TASK!r}];
g.tqOpenDrawer('p', 't1');
await g.tqDrawerWhy();
const shown = [[doc.getElementById('gen-modal-body').innerHTML, doc.getElementById('gen-modal-title').textContent]];
console.log('RESULT:' + JSON.stringify({{ shown, url: g.fetches.map(f => f.url).find(u => u.endsWith('/why')) }}));
"""
    )
    msg, title = out["shown"][0]
    assert "waiting on dependency p/up" in msg and "blocked" in msg and "p/t1" in title
    assert msg.count("drawer.whyNeedFree") == 2  # CPU and RAM; VRAM is not declared
    assert out["url"] == "/api/tasks/p/t1/why"


def test_drawer_shows_metric_sparklines_for_latest_run():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true, routes: {{
  '/api/runs/7': () => ({{ run_id: 7, metrics: {{ loss: 0.2, _series: {{ loss: [[1, 0.9], [2, 0.5], [3, 0.2]] }} }} }}),
}} }});
await g.flush();
S.queue = [{TASK!r}];
g.tqOpenDrawer('p', 't1');
await g.flush(); await g.flush();
const body = doc.getElementById('tq-drawer-body').innerHTML;
console.log('RESULT:' + JSON.stringify({{ svg: g.metricSparkSvg([[1, 1]]), body }}));
"""
    )
    assert out["svg"] == ""  # one point: nothing to draw
    assert "tq-metric-row" in out["body"] and "<polyline" in out["body"] and ">0.2<" in out["body"]


def test_drawer_metric_sparkline_overlays_previous_run():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true, routes: {{
  '/api/tasks/p/t1/runs': () => ({{ runs: [{{ run_id: 7 }}, {{ run_id: 5 }}, {{ run_id: 3 }}] }}),
  '/api/runs/7': () => ({{ run_id: 7, metrics: {{ _series: {{ loss: [[1, 0.9], [2, 0.5], [3, 0.2]], acc: [[1, 0.1], [2, 0.4]] }} }} }}),
  '/api/runs/5': () => ({{ run_id: 5, metrics: {{ _series: {{ loss: [[1, 1.2], [2, 0.8], [3, 0.6], [4, 0.4]] }} }} }}),
}} }});
await g.flush();
S.queue = [{TASK!r}];
g.tqOpenDrawer('p', 't1');
for (let i = 0; i < 4; i++) await g.flush();
const body = doc.getElementById('tq-drawer-body').innerHTML;
const rows = body.split('tq-metric-row').slice(1);
const loss = rows.find(r => r.includes('>loss<')) || '', acc = rows.find(r => r.includes('>acc<')) || '';
const r = {{
  prevRun: S.drawerPrevSeries && S.drawerPrevSeries.runId,
  lossDashed: loss.includes('tq-metric-prev-line'), lossPrev: loss.includes('drawer.metricPrev'),
  accDashed: acc.includes('tq-metric-prev-line'), accPrev: acc.includes('drawer.metricPrev'),
  fetched: g.fetches.map(f => f.url).filter(u => u.startsWith('/api/runs/')).sort(),
  // shared y-scale: the previous run's 1.2 is the top, so the current 0.9 starts below the top edge
  shared: g.metricSparkSvg([[1, 0.9], [2, 0.2]], 100, 24, [[1, 1.2], [2, 0.4]]),
}};
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["prevRun"] == 5
    assert out["lossDashed"] and out["lossPrev"]
    assert not out["accDashed"] and not out["accPrev"]  # key absent in the previous run
    assert out["fetched"] == ["/api/runs/5", "/api/runs/7"]
    assert 'points="0.0,2.0 100.0,' in out["shared"] and 'points="0.0,8.0 100.0,22.0"' in out["shared"]


def test_info_tab_shows_eta_left_at_stop_but_not_for_completed():
    eta = {"remaining_sec": 7500, "at_stop": True, "progress_pct": 25, "basis": "rate"}
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true }});
await g.flush();
S.queue = [{json.dumps({**TASK, "status": "stopped", "eta": eta})}];
const r = {{}};
g.tqOpenDrawer('p', 't1');
r.stopped = doc.getElementById('tq-drawer-body').innerHTML;
S.drawerTask.status = 'completed';
g.tqPaintDrawerBody();
r.completed = doc.getElementById('tq-drawer-body').innerHTML;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert "drawer.eta" in out["stopped"] and "eta.atStop" in out["stopped"] and "eta.basis.rate" in out["stopped"]
    assert "drawer.eta" not in out["completed"]
