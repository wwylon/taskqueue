"""Behavior tests for core/refresh.js (status pill/health, scoped status, refresh cycle, offline, SSE messages)."""
from __future__ import annotations

import json

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

HEALTH = {"level": "amber", "icons": {"level": {"amber": "A!"}, "parts": {"fail": "F"}},
          "parts": [{"kind": "fail", "count": 2}, {"kind": "frozen", "count": 1}],
          "counts": {"new_failed": 2}, "new_failed": ["p/a"], "interrupted": []}
DASH = {
    "status": {"running": [{"project": "a", "task_id": "x", "run_id": 1}], "config": {"max_running_tasks": 4},
               "status_counts": {"running": 1, "queued": 2, "failed": 3, "completed": 4}, "projects": ["a", "b"],
               "version": "9.9", "schema_version": 7, "git_sha": "abc", "health": HEALTH, "alert_firing": 2},
    "queue": {"tasks": [{"project": "a", "task_id": "q1", "status": "queued", "priority": 1}], "total": 42},
    "history": None, "workers": None, "tags": {"tags": ["gpu"], "tag_projects": {"gpu": ["a"]}},
}


def test_status_pill_counts_title_health_badge_and_version():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true }});
await g.flush();
doc.add('daemon-pill'); doc.add('live-dot');
const $ = id => doc.getElementById(id);
const r = {{}};
S.selectedProject = 'proj';
g._updateStatusPill({json.dumps(DASH["status"])});
r.counts = ['sc-running', 'sc-queued', 'sc-failed', 'sc-completed'].map(id => $(id).textContent);
r.title = doc.title;
r.pill = [$('daemon-pill').className, $('daemon-pill').textContent];
r.health = [$('health-pill').className, $('health-pill').textContent, $('health-pill').hidden, $('health-ack').hidden, $('health-pill').title.includes('p/a')];
r.badge = [$('nav-alerts-badge').textContent, $('nav-alerts-badge').hidden];
r.version = $('version-badge').textContent;
g._updateStatusPill(null);                       // junk payloads are ignored
g._updatePageTitle({{ queued_count: 5 }});       // queued falls back to queued_count when no status_counts
r.fallbackTitle = doc.title;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["counts"] == ["1", "2", "3", "4"]
    assert out["title"] == "A! tq 1/3/2 proj"
    assert out["pill"][0] == "status-pill pill-ok" and "status.nRunning" in out["pill"][1]
    assert out["health"][0] == "health-pill health-amber" and out["health"][1].startswith("A! F2")
    assert "F1" not in out["health"][1]                    # frozen projects are not shown in the pill
    assert out["health"][2:] == [False, False, True]
    assert out["badge"] == ["2", False]
    assert out["version"] == "tq v9.9 · db v7 · abc"
    assert out["fallbackTitle"] == "tq 0/0/5 proj"


def test_scope_payload_filters_running_and_keeps_scoped_counts():
    out = run_js(
        EV
        + """
const { g, S } = await setup({ app: true });
await g.flush();
const r = {};
const fleet = { running: [{ project: 'a' }, { project: 'b' }], status_counts: { queued: 99 }, queued_count: 99 };
S.selectedProject = 'all'; S.queueProjectFilter = 'all';
r.noScope = g._scopeStatusPayload(fleet) === fleet;
S.queueProjectFilter = 'b';
r.scopeFromFilter = g._statusScopeProject();
S.selectedProject = 'a'; S.status = { status_counts: { queued: 3 }, queued_count: 3 };
const o = g._scopeStatusPayload(fleet);
r.scoped = [o.running.map(t => t.project), o.status_counts.queued, o.queued_count];
S.status = { status_counts: { queued: 8 } };
r.derived = g._scopeStatusPayload(fleet).queued_count;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["noScope"] is True and out["scopeFromFilter"] == "b"
    assert out["scoped"] == [["a"], 3, 3] and out["derived"] == 8


def test_refresh_builds_query_applies_payload_and_resolves_waiters():
    out = run_js(
        EV
        + f"""
const {{ g, doc, S }} = await setup({{ app: true, routes: {{ '/api/dashboard': {json.dumps(DASH)} }} }});
await g.flush();
const r = {{}};
S.selectedProject = 'a'; S.queueStatusFilter = 'failed'; S.tagFilter = 'gpu'; S.queuePage = 2; S.dashboardEtag = '';
doc.getElementById('queue-search').value = ' foo ';
let resolved = 0; S.refreshResolvers.push(() => resolved++);
g.fetches.length = 0;
await g.refresh();
const u = g.fetches.find(f => f.url.startsWith('/api/dashboard')).url;
r.query = u.split('?')[1].split('&');
r.state = [S.queue.map(t => t.task_id), S.queueTotal, S.projects, S.allTags, S.allTagProjects, S.sseState, resolved];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    q = out["query"]
    for part in ("project=a", "status=failed", "tag=gpu", "search=foo", "sort_dir=asc", "tab=queue"):
        assert part in q, (part, q)
    assert any(p.startswith("limit=") for p in q) and any(p.startswith("offset=") for p in q)
    assert out["state"][0] == ["q1"] and out["state"][1] == 42 and out["state"][2] == ["a", "b"]
    assert out["state"][3] == ["gpu"] and out["state"][4] == {"gpu": ["a"]}
    assert out["state"][5] == "live" and out["state"][6] == 1


def test_three_failures_go_offline_and_a_success_recovers_and_overlap_coalesces():
    out = run_js(
        EV
        + f"""
let ok = false;
const {{ g, doc, S }} = await setup({{ app: true, routes: {{ '/api/dashboard': () => (ok ? {json.dumps(DASH)} : null) }} }});
await g.flush();
doc.add('daemon-pill'); doc.add('live-dot');
const r = {{}};
const err = console.error; console.error = () => {{}};
const hdr = () => doc.getElementById('main-header').style.marginTop;
const dash = () => g.fetches.filter(f => f.url.startsWith('/api/dashboard')).length;
S.dashboardEtag = '';
await g.refresh(); await g.refresh(); await g.refresh();   // (the boot refresh already failed once)
r.afterThree = [hdr(), S.sseState, doc.getElementById('daemon-pill').className];
ok = true; S.dashboardEtag = '';
await g.refresh();
r.recovered = [hdr(), S.sseState, doc.getElementById('live-dot').classList.has('live')];
// a call during an in-flight refresh is coalesced into exactly one re-run
const n = dash();
const p1 = g.refresh(); const p2 = g.refresh(); const p3 = g.refresh();
await Promise.all([p1, p2, p3]); await g.flush(); await g.flush();
r.coalesced = dash() - n;
console.error = err;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["afterThree"][:2] == ["36px", "error"] and "pill-fail" in out["afterThree"][2]
    assert out["recovered"] == ["0", "live", True]
    assert out["coalesced"] == 2


def test_sse_message_updates_status_and_ignores_junk():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true });
await g.flush();
doc.add('daemon-pill'); doc.add('live-dot');
const r = {};
const es = g.eventSources.find(e => e.url.startsWith('/api/events'));
es.emit('not json'); es.emit(JSON.stringify({ hello: 1 }));          // junk and non-status frames are ignored
r.ignored = !!(S.status && S.status.hello === 1);
S.sseState = 'error';
es.emit(JSON.stringify({ running: [{ project: 'a', task_id: 'x', run_id: 5 }], config: { max_running_tasks: 2 },
  status_counts: { running: 1, queued: 0, failed: 0 }, projects: ['a'] }));
r.state = [S.status.running.length, S.cfg.max_running_tasks, S.projects, S.sseState];
r.dot = doc.getElementById('sse-dot').title;
r.pill = doc.getElementById('daemon-pill').textContent;
// structural membership change → soon refresh (400ms); metrics-only bumps use ≥15s throttle
r.queued = g.timers.filter(t => t.live && t.ms >= 400 && t.ms <= 15000).length > 0;
es.onopen();
r.open = S.sseState;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["ignored"] is False
    assert out["state"] == [1, 2, ["a"], "live"]
    assert out["dot"] == "sse.live" and "status.nRunning" in out["pill"]
    assert out["queued"] is True and out["open"] == "live"


def test_sse_metrics_only_throttles_full_queue_refresh():
    """Same running set + data_version bump → ≥15s throttle, not an immediate 400ms refresh."""
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true });
await g.flush();
doc.add('daemon-pill'); doc.add('live-dot');
const es = g.eventSources.find(e => e.url.startsWith('/api/events'));
const snap = { running: [{ project: 'a', task_id: 'x', run_id: 5, elapsed_sec: 1, progress_pct: 10 }],
  config: { max_running_tasks: 2 }, status_counts: { running: 1, queued: 0, failed: 0 },
  projects: ['a'], data_version: 2 };
// Pretend membership already matches so this frame is metrics-only.
S.prevRunningStr = g._runningIdentity(snap.running, snap.config);
S.prevGaugeStructStr = g._gaugeStructureKey(snap, snap.config);
S.lastDataVersion = 1;
S.queue = [{ project: 'a', task_id: 'x', status: 'running', priority: 50, run_id: 5 }];
g.timers.splice(0);
es.emit(JSON.stringify(snap));
const waits = g.timers.filter(t => t.live).map(t => t.ms);
console.log('RESULT:' + JSON.stringify({ waits, min: waits.length ? Math.min(...waits) : null }));
"""
    )
    assert out["waits"], out
    assert out["min"] >= 1000  # throttled path (not forceSoon 400)
