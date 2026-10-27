"""Behavior tests for views/groups.js (Groups tab + queue group chips) under the Node DOM stub."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

GROUPS = [
    {"name": "zeta", "total": 3, "by_status": {"queued": 2, "failed": 1}, "projects": ["p1"]},
    {"name": "alpha", "total": 5, "by_status": {"running": 5}, "projects": ["p1", "p2"]},
    {"name": "", "project": "p2", "total": 2, "by_status": {"completed": 2}},
]


def test_load_groups_renders_sorted_rows_with_scoped_actions():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true, routes: {{ '/api/groups': {{ groups: {GROUPS!r} }} }} }});
await g.flush();
S.selectedProject = 'p2';
await g.loadGroups(); await g.flush();
const el = doc.getElementById('groups-content');
const rows = el.querySelectorAll('strong').map(s => {{
  const tags = s.querySelectorAll('.proj-tag').map(t => t.textContent);
  return {{ label: (s.textContent || '').trim(), tags }};
}});
const clicks = el.querySelectorAll('button').map(b => JSON.parse(b.getAttribute('data-click')));
const r = {{ rows, calls: clicks.map(c => c[0].slice(0, 5)), html: el.innerHTML }};
r.fetchUrl = g.fetches.filter(f => f.url.startsWith('/api/groups')).pop().url;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    # empty name sorts first; project names are colored .proj-tag spans (stub textContent is direct text only)
    assert out["rows"] == [
        {"label": "(ungrouped)", "tags": ["p2"]},
        {"label": "alpha", "tags": ["p1", "p2"]},
        {"label": "zeta", "tags": ["p1"]},
    ]
    assert out["html"].count('class="proj-tag"') >= 3
    assert out["fetchUrl"] == "/api/groups?project=p2"
    # per row: view, stop, retry failed, restart all
    assert out["calls"][0] == ["viewGroupInQueue", "", "p2"]
    assert [c[0] for c in out["calls"][:4]] == ["viewGroupInQueue", "groupStop", "groupRetry", "groupRetry"]
    assert out["calls"][1][2:] == ["", "p2"]          # bulk ops scoped to the ungrouped row's project
    assert out["calls"][3][2:] == ["", True, "p2"]    # restart all = includeCompleted


def test_load_groups_empty_and_error_states():
    out = run_js(
        """
const { g, doc, S } = await setup({ app: true, routes: { '/api/groups': { groups: [] } } });
await g.flush();
await g.loadGroups(); await g.flush();
const el = doc.getElementById('groups-content');
const r = { empty: el.innerHTML.includes('groups.noGroups') };
g.fetch = async () => { throw new Error('boom'); };
await g.loadGroups();
r.err = el.innerHTML.includes('Error: Error: boom');
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out == {"empty": True, "err": True}


def test_group_stop_confirms_then_posts_scoped_request():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true, routes: { '/api/groups/g%201/stop': { stopped: 4 }, '/api/groups': { groups: [] } } });
await g.flush();
const r = { alerts: [] };
let confirm = null;
g.openConfirm = (msg, ok, opts) => { r.msg = r.msg || msg; confirm = { ok, opts }; };
g.openAlert = m => r.alerts.push(m);
const btn = doc.createElement('button'); btn.textContent = 'Stop all';
g.groupStop(btn, 'g 1', 'p1');
r.pending = btn.disabled;
g.groupStop(btn, 'g 1', 'p1');                       // second click while pending: ignored
await g.flush();
r.asked = !!confirm;
g.fetches.length = 0;
await confirm.ok(); await g.flush();
const post = g.fetches.find(f => f.url.includes('/stop'));
r.url = post && post.url; r.method = post && post.opts.method;
r.released = btn.disabled === false;
// cancelling releases the button too
btn.disabled = false;
g.groupStop(btn, '', '');
confirm.opts.onCancel();
r.cancelReleased = btn.disabled === false;
r.ungroupedMsg = r.msg;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["pending"] is True and out["asked"] is True
    assert 'group "g 1" of project p1' in out["msg"]
    assert out["url"] == "/api/groups/g%201/stop?project=p1" and out["method"] == "POST"
    assert out["alerts"] and out["released"] is True and out["cancelReleased"] is True


def test_group_retry_restart_all_requires_dry_run_count_confirmation():
    out = run_js(
        EV
        + """
const calls = [];
const { g, doc } = await setup({ app: true, routes: {
  '/api/groups/g1/retry': (url) => { calls.push(url); return url.includes('dry_run=1') ? { ok: true, would_retry: 7, completed_included: 3 } : { ok: true, retried: 7 }; },
  '/api/groups': { groups: [] },
} });
await g.flush();
const r = { alerts: [] };
g.openAlert = m => r.alerts.push(m);
g.refresh = async () => {};
let confirm = null;
g.openConfirm = (msg, ok, opts) => { r.msg = r.msg || msg; confirm = { ok, opts }; };
const btn = doc.createElement('button');
const p = g.groupRetry(btn, 'g1', true, 'p1');
for (let i = 0; i < 6; i++) await g.flush();
r.beforeConfirm = calls.slice();
confirm.ok();
await p;
r.all = calls.slice();
// plain "retry failed" skips the dry run entirely
calls.length = 0;
await g.groupRetry(doc.createElement('button'), 'g1', false, '');
r.plain = calls.slice();
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert len(out["beforeConfirm"]) == 1 and "dry_run=1" in out["beforeConfirm"][0]
    assert "7 task(s)" in out["msg"] and "3 COMPLETED" in out["msg"]
    q = out["all"][1]
    assert "confirm=7" in q and "completed=1" in q and "project=p1" in q and "dry_run" not in q
    assert out["plain"] == ["/api/groups/g1/retry"]
    assert len(out["alerts"]) == 2


def test_group_chips_filter_toggle_and_view_in_queue():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true, routes: {{ '/api/groups': {{ groups: {GROUPS!r} }} }} }});
await g.flush();
const bar = doc.getElementById('queue-group-bar');
const r = {{}};
await g._populateGroupChips();
r.collapsed = bar.querySelectorAll('.filter-chip').length;
fire(doc, 'click', bar.querySelector('.filter-label'));          // expand
r.chips = bar.querySelectorAll('.filter-chip').map(b => b.getAttribute('data-group'));
let refreshed = 0; g.refresh = async () => {{ refreshed++; }};
S.queuePage = 4;
fire(doc, 'click', bar.querySelectorAll('.filter-chip').find(b => b.getAttribute('data-group') === 'alpha'));
r.filter = S.groupFilter; r.page = S.queuePage; r.refreshed = refreshed;
r.active = bar.querySelectorAll('.filter-chip').filter(b => b.classList.has('active')).map(b => b.getAttribute('data-group'));
fire(doc, 'click', bar.querySelectorAll('.filter-chip').find(b => b.getAttribute('data-group') === ''));
r.cleared = S.groupFilter;
// "View in Queue" from the Groups tab: group set first, project switched with keepGroup, queue tab opened
g.viewGroupInQueue('', 'p2');
r.ungrouped = S.groupFilter;
await g.flush(); await g.flush();
r.project = S.selectedProject;
r.page2 = (doc.querySelector('.page.active') || {{}}).id;
r.expanded = bar.dataset.expanded;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["collapsed"] == 0
    assert out["chips"] == ["", "alpha", "zeta", "__ungrouped__"] or out["chips"][:1] == [""]
    assert out["filter"] == "alpha" and out["page"] == 0 and out["refreshed"] >= 1
    assert out["active"] == ["alpha"]
    assert out["cleared"] is None
    assert out["ungrouped"] == "__ungrouped__"
    assert out["project"] == "p2" and out["page2"] == "queue" and out["expanded"] == "1"
