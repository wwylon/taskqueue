"""Behavior tests for components/taskactions.js (task buttons, priority, project freeze/stop-all, worker sync)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: ROUTES });
await g.flush();
const toasts = []; g.showToast = (m, k) => toasts.push([m, k]);
const alerts = []; g.openAlert = (m, t) => alerts.push([m, t || null]);
const posts = () => g.fetches.filter(f => f.opts && f.opts.method).map(f => [f.opts.method, f.url]);
const btn = () => { const b = doc.createElement('button'); b.textContent = 'go'; return b; };
const r = {};
"""


def _js(routes: str, body: str) -> str:
    return EV + PRE.replace("ROUTES", routes) + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_button_pending_blocks_double_click_and_self_heals_after_timeout():
    out = run_js(_js("{}", """
const b = btn();
r.first = g._btnPending(b, 'busy');
r.state = [b.disabled, b.textContent];
r.second = g._btnPending(b, 'again');                 // disabled button refuses a second action
g.fireTimers(8000);                                    // safety timeout re-enables it
r.healed = [b.disabled, b.textContent];
g._btnPending(b); g._btnDone(b);
r.done = [b.disabled, b.textContent];
r.none = [g._btnPending(null), g._btnDone(null)];
"""))
    assert out["first"] is True and out["state"] == [True, "busy"] and out["second"] is False
    assert out["healed"] == [False, "go"] and out["done"] == [False, "go"]
    assert out["none"][0] is False


def test_stop_retry_mark_processed_post_to_endpoints_and_toast_errors():
    out = run_js(_js("{ '/api/stop/': { ok: true }, '/api/retry/p/t%201': { ok: true }, '/api/mark-processed/': { ok: true } }", """
g.tqStopUndoMs = 0;
S.status = { running: [] }; S.queue = [{ project: 'p', task_id: 't 1', status: 'failed' }];
let b = btn();
await g.stopTask(b, 'p', 't 1');
r.stop = [toasts.pop(), b.disabled, posts().pop()];
b = btn();
await g.retryTask('p', 't 1', b);
r.retry = [toasts.pop(), S.queue[0].status, b.disabled, posts().pop()];
b = btn();
await g.markProcessed(b, 'p', 't 1');
r.mark = [posts().pop(), b.disabled];
// a failing server shows an error toast and still releases the button
g.fetch = async () => ({ ok: false, status: 500, json: async () => ({ error: 'boom' }) });
b = btn();
await g.stopTask(b, 'p', 'x');
r.fail = [toasts.pop(), b.disabled];
b = btn();
await g.retryTask('p', 'x', b);
r.failRetry = [toasts.pop(), b.disabled];
"""))
    assert out["stop"] == [["toast.stopped", "ok"], False, ["POST", "/api/stop/p/t%201"]]
    assert out["retry"] == [["toast.retried", "ok"], "queued", False, ["POST", "/api/retry/p/t%201"]]
    assert out["mark"] == [["POST", "/api/mark-processed/p/t%201"], False]
    assert out["fail"] == [["boom", "err"], False] and out["failRetry"] == [["boom", "err"], False]


def test_retry_new_and_enable_use_ok_flag_and_alert_on_failure():
    out = run_js(_js("{}", """
let mode = 'ok'; const seen = [];
g.fetch = async (url, opts) => (seen.push(url), { ok: mode === 'ok', status: 200, json: async () => (mode === 'ok' ? { ok: true } : { error: 'nope' }) });
const row = doc.add('qrow-1-p-t1'); const badge = doc.createElement('span'); badge.className = 'badge badge-failed'; badge.textContent = 'failed';
row.appendChild(badge);
let b = btn();
await g.retryTaskNew('p', 't1', b);
r.badge = [badge.className, badge.textContent, b.disabled, alerts.length];
mode = 'bad';
await g.retryTaskNew('p', 't1', btn());
await g.enableTask('p', 't1', btn());
r.alerts = alerts.map(a => a[0]);
mode = 'ok';
await g.enableTask('p', 't1', btn());
r.enableOk = alerts.length;
await g.setPriority('p', 't 1', '7.9');
r.prio = seen.filter(u => u.startsWith('/api/priority/')).pop();
"""))
    assert out["badge"] == ["badge badge-queued", "queued", False, 0]
    assert out["alerts"] == ["nope", "nope"] and out["enableOk"] == 2
    assert out["prio"] == "/api/priority/p/t%201/7"


def test_force_asks_for_confirmation_posts_on_confirm_and_releases_on_cancel():
    out = run_js(_js("{ '/api/force/': { ok: true } }", """
S.status = { running: [] }; S.queue = [{ project: 'p', task_id: 't', status: 'queued' }];
let call = null;
g.openConfirm = (msg, cb, opts) => { call = { msg, cb, opts }; };
let b = btn();
g.forceTask(b, 'p', 't');
r.pending = [b.disabled, b.textContent, call.msg, call.opts.confirmLabel];
g.forceTask(b, 'p', 't');                               // second click while pending does nothing
call.opts.onCancel();
r.cancelled = [b.disabled, posts().length];
b = btn();
g.forceTask(b, 'p', 't');
await call.cb();
r.confirmed = [posts().pop(), S.queue[0].status, toasts.pop(), b.disabled];
// server refusal: toast + alert, button released
g.fetch = async () => ({ ok: false, status: 409, json: async () => ({ error: 'locked' }) });
b = btn();
g.forceTask(b, 'p', 't');
await call.cb();
r.refused = [toasts.pop(), alerts.pop()[0], b.disabled];
"""))
    assert out["pending"] == [True, "btn.forcing", "confirm.forceRun", "btn.force"]
    assert out["cancelled"] == [False, 0]
    assert out["confirmed"] == [["POST", "/api/force/p/t"], "queued", ["toast.forceWaiting", "info"], False]
    assert out["refused"] == [["locked", "err"], "locked", False]


def test_priority_helpers_clamp_and_enable_toggle_and_inline_editor():
    out = run_js(_js("{ '/api/priority/': { ok: true }, '/api/enable/': { ok: true }, '/api/tasks/': { ok: true } }", """
g._dagRefreshAfterAction = () => { r.dag = (r.dag || 0) + 1; };
await g.tqSetPriority('p', 't', '250');
await g.tqSetPriority('p', 'u', 'abc');
const urls = g.fetches.map(f => f.url).filter(u => u.startsWith('/api/priority/'));
r.urls = urls;
await g.tqSetEnabled('p', 't', false);
await g.tqSetEnabled('p', 't', true);
r.enable = g.fetches.map(f => f.url).filter(u => u.startsWith('/api/enable/'));
r.dag = r.dag;
const prompts = [];
g.window.prompt = (m, d) => { prompts.push(d); return '42'; };
g.tqPromptPriority('p', 't', 5);
await g.flush();
r.prompted = [prompts, g.fetches.filter(f => f.url.startsWith('/api/priority/')).pop().url];
g.window.prompt = () => null;
const n = g.fetches.length; g.tqPromptPriority('p', 't', 5); r.cancel = g.fetches.length === n;
// inline editor: replaces the span with an input; a valid value is posted and stored, an invalid one reverts
S.queue = [{ project: 'p', task_id: 't', status: 'queued', priority: 5 }];
g.renderQueue = () => { r.rendered = (r.rendered || 0) + 1; };
const holder = doc.add('prio-holder'); const span = doc.createElement('span'); holder.appendChild(span);
g.startPrioEdit(span, 'p', 't', 5);
const inp = holder.children[0];
r.input = [inp.tagName, inp.value, holder.children.length];
inp.value = '77';
await inp.listeners.blur[0]();
r.saved = [S.queue[0].priority, g.fetches.filter(f => f.url.startsWith('/api/')).filter(f => f.opts && f.opts.method).pop().url, r.rendered];
const span2 = doc.createElement('span'); const holder2 = doc.add('prio-holder2'); holder2.appendChild(span2);
g.startPrioEdit(span2, 'p', 't', 9);
const inp2 = holder2.children[0]; inp2.value = '999';
await inp2.listeners.blur[0]();
r.reverted = [holder2.children[0].className, holder2.children[0].textContent, S.queue[0].priority];
"""))
    assert out["urls"] == ["/api/priority/p/t/100", "/api/priority/p/u/0"]
    assert out["enable"] == ["/api/enable/p/t?enabled=0", "/api/enable/p/t?enabled=1"]
    assert out["dag"] == 4 or out["dag"] >= 2
    assert out["prompted"] == [["5"], "/api/priority/p/t/42"] and out["cancel"] is True
    assert out["input"] == ["INPUT", 5, 1]
    assert out["saved"] == [77, "/api/tasks/p/t/priority", 1]
    assert out["reverted"] == ["prio-span", 9, 77]


def test_project_freeze_button_toggle_and_stop_all_confirm():
    out = run_js(_js("{ '/api/project/': { ok: true, stopped: 3 } }", """
doc.add('proj-freeze-btn', { tag: 'button' }); doc.add('proj-stopall-btn', { tag: 'button' });
S.selectedProject = 'all';
await g.toggleProjectFreeze(); g.stopAllProject();
r.noop = posts().length;                                   // no project selected: nothing happens
S.selectedProject = 'p'; S.cfg = { project_frozen_p: false };
g._updateFreezeBtn();
r.label = doc.getElementById('proj-freeze-btn').textContent;
await g.toggleProjectFreeze();
r.freeze = [posts().pop(), toasts.pop()];
S.cfg = { project_frozen_p: true };
g._updateFreezeBtn();
r.frozen = [doc.getElementById('proj-freeze-btn').textContent, doc.getElementById('proj-freeze-btn').style.color];
await g.toggleProjectFreeze();
r.unfreeze = [posts().pop(), toasts.pop()];
let call = null;
g.openConfirm = (msg, cb, opts) => { call = { msg, cb, opts }; };
g.stopAllProject();
r.confirm = [call.msg, call.opts.confirmLabel, doc.getElementById('proj-stopall-btn').disabled];
await call.cb();
r.stopped = [posts().pop(), alerts.pop()[0], doc.getElementById('proj-stopall-btn').disabled];
"""))
    assert out["noop"] == 0
    assert out["label"] == "btn.freeze"
    assert out["freeze"] == [["POST", "/api/project/freeze/p"], ["toast.projectBlocked", "ok"]]
    assert out["frozen"] == ["btn.unfreeze", "var(--ok)"]
    assert out["unfreeze"] == [["POST", "/api/project/unfreeze/p"], ["toast.projectUnblocked", "ok"]]
    assert out["confirm"][1:] == ["btn.stopAll", True] and '"p"' in out["confirm"][0]
    assert out["stopped"] == [["POST", "/api/project/stop-all/p"], "alert.stoppedTasks", False]


def test_project_root_bar_and_worker_sync_alerts():
    out = run_js(_js("{ '/api/project-roots': { p: 'D:/work/p' }, '/api/worker/sync/': { ok: true } }", """
const bar = doc.add('proj-root-bar'); const txt = doc.add('proj-root-text');
S.projectRoots = {}; S.cfg = {};
await g._showProjectRoot('p');
r.root = [txt.textContent, bar.style.display];
await g._showProjectRoot('ghost');
r.missing = txt.textContent.includes('no registered root path');
S.cfg = { project_frozen_p: true };
await g._showProjectRoot('p');
r.frozen = [txt.textContent.includes('freeze.blockedBanner'), bar.style.color];
await g._showProjectRoot('all');
r.hidden = bar.style.display;
let b = btn();
await g.workerSync(b, 'w 1');
r.sync = [posts().pop(), alerts.pop()[0].includes('Sync queued'), b.disabled];
g.fetch = async () => ({ ok: true, json: async () => ({ ok: false, error: 'offline' }) });
await g.workerSync(btn(), 'w1');
r.syncFail = alerts.pop()[0];
"""))
    assert out["root"] == ["📁 p root: D:/work/p", ""]
    assert out["missing"] is True
    assert out["frozen"] == [True, "var(--warn)"] and out["hidden"] == "none"
    assert out["sync"] == [["POST", "/api/worker/sync/w%201"], True, False]
    assert out["syncFail"] == "Sync failed: offline"


def test_stop_waits_for_undo_window_and_undo_cancels():
    out = run_js(_js("{ '/api/stop/': { ok: true } }", """
S.status = { running: [] }; S.queue = [];
g.tqStopUndoMs = 5000;
let b = btn();
const p1 = g.stopTask(b, 'p', 'a');
await g.flush();
const before = posts().length;
g.fireTimers(5000); await p1;                     // grace period over: stop is sent
r.sent = [before, posts().pop()];
b = btn();
const p2 = g.stopTask(b, 'p', 'b');
await g.flush();
const host = doc.getElementById('tq-toast-host');
const undo = host.children[host.children.length - 1].children[1];
undo.onclick({ stopPropagation() {} });            // Undo before the timer fires
await p2;
r.undone = [posts().filter(x => x[1].endsWith('/p/b')).length, toasts.pop(), b.disabled];
"""))
    assert out["sent"] == [0, ["POST", "/api/stop/p/a"]]
    assert out["undone"] == [0, ["toast.stopUndone", "info"], False]


def test_rerun_from_history_confirms_then_posts():
    out = run_js(_js("{ '/api/runs/9/rerun': { ok: true } }", """
S.status = { running: [] }; S.queue = [];
const asked = []; g.openConfirm = (m, ok) => { asked.push(m); return ok(); };
await g.tqRerunRun(9);
await g.flush();
r.asked = asked.length; r.post = posts().pop(); r.toast = toasts.pop();
"""))
    assert out["asked"] == 1 and out["post"] == ["POST", "/api/runs/9/rerun"]
    assert out["toast"] == ["toast.retried", "ok"]
