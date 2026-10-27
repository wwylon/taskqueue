"""Behavior tests for core/loader.js (header project, tab loader dispatch, tqLoop single-flight/back-off, tqBusy)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: {} });
await g.flush();
const r = {};
"""


def _js(body: str) -> str:
    return EV + PRE + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_header_project_and_query_encode_selection():
    out = run_js(_js("""
S.selectedProject = 'all'; r.all = [g.tqHeaderProject(), g.tqProjectQuery()];
S.selectedProject = ''; r.none = g.tqProjectQuery();
S.selectedProject = 'a b'; r.one = [g.tqHeaderProject(), g.tqProjectQuery()];
"""))
    assert out["all"] == ["", ""] and out["none"] == "" and out["one"] == ["a b", "?project=a%20b"]


def test_load_tab_dispatches_to_the_page_loader():
    out = run_js(_js("""
const calls = [];
for (const [tab, fn] of [['deps', 'loadDeps'], ['tasks', 'loadTaskStats'], ['timeline', 'loadTimeline'], ['schedule', 'loadSchedules'],
  ['alerts', 'loadAlerts'], ['audit', 'loadAudit'], ['api', 'loadApiTokens'], ['docs', 'loadDocsPage']]) {
  g[fn] = () => { calls.push(fn); return fn; };
  r[tab] = g.tqLoadTab(tab);
}
r.unknown = g.tqLoadTab('nope') === undefined;
"""))
    assert out["deps"] == "loadDeps" and out["docs"] == "loadDocsPage" and out["tasks"] == "loadTaskStats"
    assert out["unknown"] is True


def test_loop_is_single_flight_reruns_once_and_backs_off_on_failure():
    out = run_js(_js("""
let n = 0; let release;
const fn = () => { n++; return new Promise(res => { release = res; }); };
const p1 = g.tqLoop('t1', fn, { force: true });
await g.flush();
const p2 = g.tqLoop('t1', fn, { force: true }); const p3 = g.tqLoop('t1', fn, { force: true });
r.inFlight = [n, p2 === p1, p3 === p1];
release(true); await p1; await g.flush(); await g.flush();
r.rerun = [n, window.__tqLoops.t1.timer !== null];            // queued "again" is deferred by the min gap, not run at once
// failure doubles the gap; success resets it
const bad = () => Promise.resolve(false);
await g.tqLoop('t2', bad, { force: true, minGapMs: 1000 });
r.gap1 = window.__tqLoops.t2.gap;
await g.tqLoop('t2', bad, { force: true, minGapMs: 1000 });
r.gap2 = window.__tqLoops.t2.gap;
await g.tqLoop('t2', () => Promise.resolve(true), { force: true, minGapMs: 1000 });
r.gap3 = window.__tqLoops.t2.gap;
await g.tqLoop('t3', () => { throw new Error('x'); }, { force: true, minGapMs: 1000 });
r.thrown = window.__tqLoops.t3.gap;
// not forced while the previous run finished moments ago: schedules a timer instead of running
let m = 0; await g.tqLoop('t4', () => { m++; }, { force: true, minGapMs: 5000 });
const before = g.timers.length;
await g.tqLoop('t4', () => { m++; }, { minGapMs: 5000 });
r.deferred = [m, g.timers.length > before];
"""))
    assert out["inFlight"] == [1, True, True] and out["rerun"] == [1, True]
    assert out["gap1"] == 2000 and out["gap2"] == 4000 and out["gap3"] == 1000 and out["thrown"] == 2000
    assert out["deferred"] == [1, True]


def test_busy_tracks_count_shows_page_spinner_once_and_returns_work():
    out = run_js(_js("""
const page = doc.getElementById('audit') || doc.add('audit');
let done; const work = new Promise(res => { done = res; });
const ret = g.tqBusy(work, 'audit');
r.sameWork = ret instanceof Promise;
r.during = [window.__tqBusy.count, page.querySelector(':scope > .tq-page-spinner') !== null];
done('v'); r.value = await ret; await g.flush();
r.after = [window.__tqBusy.count, window.__tqBusy.loaded.audit, page.querySelector(':scope > .tq-page-spinner')];
await g.tqBusy(Promise.reject(new Error('x')), 'audit').catch(() => {});
await g.flush();
r.afterFail = window.__tqBusy.count;
const hidden = {}; g._showPageEmpty('nope-a', 'nope-b', true); r.noEls = true;
const e = doc.add('pe-empty'), w = doc.add('pe-wrap');
g._showPageEmpty('pe-empty', 'pe-wrap', true); r.on = [e.hidden, w.style.display];
g._showPageEmpty('pe-empty', 'pe-wrap', false); r.off = [e.hidden, w.style.display];
"""))
    assert out["sameWork"] is True and out["during"][0] == 1
    assert out["value"] == "v" and out["after"][0] == 0 and out["after"][1] is True
    assert out["afterFail"] == 0 and out["noEls"] is True
    assert out["on"] == [False, "none"] and out["off"] == [True, ""]
