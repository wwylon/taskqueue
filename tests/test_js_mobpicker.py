"""Behavior tests for components/mobpicker.js (mobile FAB task picker)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: {} });
await g.flush();
const toasts = []; g.showToast = (m, k) => toasts.push([m, k]);
const r = {};
doc.add('mob-task-picker'); doc.add('mob-task-list'); doc.add('mob-task-search');
"""


def _js(body: str) -> str:
    return EV + PRE + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_picker_lists_deduped_filtered_tasks_and_handles_empty():
    out = run_js(_js(r"""
const byStatus = {
  queued: { tasks: [{ project: 'p', task_id: 'a', status: 'queued', description: 'Train <x>' }] },
  failed: [{ project: 'p', task_id: 'a', status: 'failed' }, { project: 'q', task_id: 'b', status: 'failed' }],
};
const urls = [];
g.fetch = async (u) => { urls.push(u); const s = u.match(/status=(\w+)/)[1]; if (s === 'stopped') throw new Error('x'); return { json: async () => byStatus[s] || { tasks: [] } }; };
const list = doc.getElementById('mob-task-list');
doc.getElementById('mob-task-search').focus = () => {};
g.openMobTaskPicker();
await g.flush(); await g.flush();
r.open = doc.getElementById('mob-task-picker').classList.has('open');
r.urls = urls.length;
r.rows = [(list.innerHTML.match(/mobForceTask/g) || []).length, list.innerHTML.includes('Train &lt;x&gt;')];
doc.getElementById('mob-task-search').value = 'q/b';
await g.renderMobTaskList();
r.filtered = [(list.innerHTML.match(/mobForceTask/g) || []).length, list.innerHTML.includes('q/')];
doc.getElementById('mob-task-search').value = 'zzz';
await g.renderMobTaskList();
r.empty = list.innerHTML.includes('mobile.noRunnableTasks');
g.closeMobTaskPicker();
r.closed = doc.getElementById('mob-task-picker').classList.has('open');
"""))
    assert out["open"] is True and out["urls"] == 7 and out["closed"] is False
    assert out["rows"] == [2, True] and out["filtered"][0] == 1 and out["empty"] is True


def test_mob_force_task_posts_closes_picker_and_reports_errors():
    out = run_js(_js("""
S.status = { running: [] }; S.queue = [{ project: 'p', task_id: 't 1', status: 'failed' }];
const picker = doc.getElementById('mob-task-picker'); picker.classList.add('open');
const posts = []; g._postJson = async (u) => { posts.push(u); return {}; };
let refreshed = 0; g.refresh = () => { refreshed++; };
await g.mobForceTask('p', 't 1');
r.ok = [posts, S.queue[0].status, toasts.pop(), picker.classList.has('open'), refreshed];
const alerts = []; g.openAlert = (m, t) => alerts.push([m, t]);
g._postJson = async () => { throw new Error('locked'); };
await g.mobForceTask('p', 't');
r.err = [toasts.pop(), alerts.pop(), refreshed];
"""))
    assert out["ok"] == [["/api/force/p/t%201"], "queued", ["toast.forced", "ok"], False, 1]
    assert out["err"] == [["locked", "err"], ["locked", "modal.forceRunTitle"], 2]


def test_older_task_list_response_does_not_overwrite_a_newer_one():
    out = run_js(_js(r"""
const gate = { first: null };
let call = 0;
g.fetch = u => {
  const n = call++;   // calls 0..6 belong to the first render, 7..13 to the second
  const tasks = [{ project: n < 7 ? 'old' : 'new', task_id: 't' + n, status: 'queued' }];
  if (n === 0) return new Promise(res => { gate.first = () => res({ json: async () => ({ tasks }) }); });
  return Promise.resolve({ json: async () => ({ tasks }) });
};
const older = g.renderMobTaskList();
const newer = g.renderMobTaskList(); await newer;
gate.first(); await older;
const html = doc.getElementById('mob-task-list').innerHTML;
r.hasNew = html.includes('new/'); r.hasOld = html.includes('old/');
"""))
    assert out["hasNew"] is True and out["hasOld"] is False
