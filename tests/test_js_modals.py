"""Behavior tests for components/modals.js (generic modal, YAML modal, metrics modal, toasts) under the Node DOM stub."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV


def test_generic_modal_confirm_cancel_dismiss_and_escape():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const bg = doc.getElementById('gen-modal');
const footer = doc.getElementById('gen-modal-footer');
const r = {};
const calls = [];
g.openConfirm('Really <b>?', () => { calls.push('ok'); }, { onCancel: () => calls.push('cancel') });
r.open = bg.classList.has('open');
r.body = doc.getElementById('gen-modal-body').innerHTML;
r.buttons = footer.children.map(b => [b.textContent, b.className]);
await footer.children[1].onclick();                 // confirm: runs, closes, no dismiss callback
r.afterOk = [bg.classList.has('open'), calls.slice()];
// Escape on an open modal dismisses it (calls onCancel once) and stops other handlers
calls.length = 0;
g.openConfirm('again', () => calls.push('ok'), { onCancel: () => calls.push('cancel') });
const ev = fire(doc, 'keydown', doc, { key: 'Escape' });
r.esc = [bg.classList.has('open'), calls.slice(), !!ev.stopped];
// Escape with no modal open is ignored
const ev2 = fire(doc, 'keydown', doc, { key: 'Escape' });
r.escIdle = !!ev2.stopped;
// async onClick keeps the button disabled until settled and shows an ellipsis
let release; const gate = new Promise(res => { release = res; });
g.openModal({ title: 'T', body: 'x', actions: [{ label: 'Go', primary: true, onClick: () => gate }] });
const btn = footer.children[0];
const p = btn.onclick();
r.during = [btn.disabled, btn.textContent];
release(); await p;
r.after = [btn.disabled, btn.textContent, bg.classList.has('open')];
// openAlert escapes the message and uses a single OK action
g.openAlert('<script>');
r.alert = [doc.getElementById('gen-modal-body').innerHTML, footer.children.length];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["open"] is True and "&lt;b&gt;" in out["body"]
    assert out["buttons"] == [["btn.cancel", "btn-cancel"], ["btn.ok", "btn-primary"]]
    assert out["afterOk"] == [False, ["ok"]]
    assert out["esc"] == [False, ["cancel"], True] and out["escIdle"] is False
    assert out["during"] == [True, "…"] and out["after"] == [False, "Go", False]
    assert "&lt;script&gt;" in out["alert"][0] and out["alert"][1] == 1


def test_yaml_modal_loads_edits_saves_with_mtime_guard_and_closes():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const reqs = [];
let mtime = '111';
g.fetch = async (url, opts) => {
  reqs.push({ url, method: (opts || {}).method || 'GET', mt: opts && opts.headers && opts.headers['X-If-Mtime-Ns'], body: opts && opts.body });
  if (opts && opts.method === 'PUT') return { ok: true, status: 200, json: async () => ({ ok: true }) };
  return { ok: true, status: 200, headers: { get: k => (k === 'X-File-Mtime-Ns' ? mtime : null) }, text: async () => 'a: 1\\n' };
};
const $ = id => doc.getElementById(id);
const r = {};
await g.openYamlModal('p 1', 't/2');
r.shown = [$('yaml-modal').style.display, $('yaml-modal-pre').textContent, $('yaml-modal-title').textContent];
r.getUrl = reqs[0].url;
g.yamlModalToggleEdit();
r.edit = [$('yaml-modal-edit').value, $('yaml-modal-edit').style.display, $('yaml-edit-btn').textContent, $('yaml-save-btn').style.display];
$('yaml-modal-edit').value = 'a: 2\\n';
mtime = '222';
await g.yamlModalSave();
const put = reqs.find(q => q.method === 'PUT');
r.put = [put.url, put.mt, put.body];
r.afterSave = [$('yaml-modal-pre').textContent, $('yaml-modal-edit').style.display, $('yaml-edit-btn').textContent, $('yaml-save-btn').disabled];
// second save uses the refreshed stamp
g.yamlModalToggleEdit(); await g.yamlModalSave();
r.mt2 = reqs.filter(q => q.method === 'PUT').pop().mt;
// a rejected save shows the server error and leaves edit mode on
g.fetch = async () => ({ ok: false, status: 409, json: async () => ({ error: 'changed on disk' }) });
g.yamlModalToggleEdit();
await g.yamlModalSave();
r.err = [$('yaml-modal-error').textContent, $('yaml-modal-error').style.display, $('yaml-modal-edit').style.display];
g.closeYamlModal();
r.closed = $('yaml-modal').style.display;
const before = reqs.length;
await g.yamlModalSave();                              // no task bound: no request
r.noop = reqs.length === before;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["shown"] == ["flex", "a: 1\n", "YAML — p 1 / t/2"]
    assert out["getUrl"] == "/api/tasks/p%201/t%2F2/yaml"
    assert out["edit"] == ["a: 1\n", "block", "Cancel", ""]
    assert out["put"] == ["/api/tasks/p%201/t%2F2/yaml", "111", "a: 2\n"]
    assert out["afterSave"] == ["a: 2\n", "none", "Edit", False]
    assert out["mt2"] == "222"
    assert out["err"] == ["changed on disk", "block", "block"]
    assert out["closed"] == "none" and out["noop"] is True


def test_yaml_modal_load_error_and_copy_feedback():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const $ = id => doc.getElementById(id);
g.fetch = async () => ({ ok: false, status: 404, statusText: 'nf', text: async () => 'missing' });
await g.openYamlModal('p', 't');
const r = { err: [$('yaml-modal-pre').textContent, $('yaml-modal-error').textContent, $('yaml-modal-error').style.display] };
$('yaml-modal-pre').textContent = 'k: v';
const written = [];
g.navigator.clipboard.writeText = async t => { written.push(t); };
const btn = doc.createElement('button'); btn.textContent = 'Copy';
$('yaml-copy-btn') ? ($('yaml-copy-btn').textContent = 'Copy') : 0;
await g.yamlModalCopy();
r.written = written;
g.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
await g.yamlModalCopy();
r.alerts = [$('gen-modal-body').innerHTML, $('gen-modal').classList.has('open')];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["err"] == ["", "Error 404: missing", "block"]
    assert out["written"] == ["k: v"]
    assert "Copy failed" in out["alerts"][0] and out["alerts"][1] is True


def test_resource_modal_tracks_run_and_compare_swaps_in_diff_table():
    out = run_js(
        EV
        + """
const DIFF = { changes: { status: { a: 'failed', b: 'completed' }, metrics: { loss: { a: 1, b: 0.5 } }, env_keys: { only_a: ['X'], only_b: [] } } };
const { g, doc, S } = await setup({ app: true, routes: { '/api/runs/diff?a=3&b=4': DIFF, '/api/runs/diff?a=4&b=4': { changes: {} } } });
await g.flush();
const $ = id => doc.getElementById(id);
S.status = { running: [{ run_id: 3, project: 'p', task_id: 't', elapsed_sec: 10 }], recent: [] };
g._usageHtml = () => '<i>usage</i>';
g._drawSparklineOnCanvas = async () => {};
const r = {};
g.showResourceModal(3);
r.title = $('resource-modal-title').textContent;
r.runId = g.getResourceModalRunId();
r.body = $('resource-modal-body').innerHTML.includes('usage') && $('resource-modal-body').innerHTML.includes('Updates automatically');
r.lastHtml = !!$('resource-modal-body').dataset.lastHtml;
r.display = $('resource-modal').style.display;
// compare: first pick only toasts, second opens the diff and detaches live updates
const toastHost = () => doc.body.children.find(c => c.id === 'tq-toast-host');
await g.compareRun(3);
r.picked = toastHost().children.map(c => c.className);
await g.compareRun(4);
const html = $('resource-modal-body').innerHTML;
r.diff = ['<table', 'metrics.loss', 'status', 'env_keys'].map(s => html.includes(s));
r.detached = [g.getResourceModalRunId(), $('resource-modal-body').dataset.lastHtml];
g.closeResourceModal();
r.closed = $('resource-modal').style.display;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["title"] == "Metrics — p/t run #3" and out["runId"] == 3
    assert out["body"] is True and out["lastHtml"] is True and out["display"] == "flex"
    assert out["picked"] == ["tq-toast tq-toast-info"]
    assert out["diff"] == [True, True, True, True]
    assert out["detached"] == [None, None] and out["closed"] == "none"


def test_toasts_stack_cap_expire_and_click_through():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const host = () => doc.getElementById('tq-toast-host') || doc.body.children.find(c => c.id === 'tq-toast-host');
const r = {};
let clicked = 0;
for (let i = 0; i < 5; i++) g.showToast('m' + i, i === 4 ? 'err' : undefined, 100, i === 4 ? () => clicked++ : null);
const kids = () => host().children;
r.count = kids().length;
r.texts = kids().map(k => k.textContent);
r.cls = kids().map(k => k.className);
r.pointer = kids()[2].style.cursor;
kids()[2].onclick();
r.clicked = clicked; r.afterClick = kids().length;
g.fireTimers(200);
r.expired = kids().length;
g.showToast('a\\nb');
r.multiline = kids()[0].style.whiteSpace;
// anchor toast: replaces the previous one on the same anchor; null anchor falls back to a normal toast
const anchor = doc.createElement('button');
anchor.getBoundingClientRect = () => ({ top: 100, left: 50, width: 20, bottom: 120 });
g.showAnchorToast(anchor, 'one'); const first = anchor._anchorToastEl;
g.showAnchorToast(anchor, 'two');
r.anchor = [first !== anchor._anchorToastEl, anchor._anchorToastEl.textContent, anchor._anchorToastEl.className.includes('tq-anchor-toast')];
const n = kids().length;
g.showAnchorToast(null, 'fallback');
r.fallback = kids().length === n + 1;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["count"] == 3 and out["texts"] == ["m2", "m3", "m4"]
    assert out["cls"][2] == "tq-toast tq-toast-err" and out["cls"][0] == "tq-toast tq-toast-info"
    assert out["pointer"] == "pointer"
    assert out["clicked"] == 1 and out["afterClick"] == 2
    assert out["expired"] == 0 and out["multiline"] == "pre-line"
    assert out["anchor"] == [True, "two", True] and out["fallback"] is True
