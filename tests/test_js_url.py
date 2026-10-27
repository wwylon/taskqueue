"""Behavior tests for core/url.js (UI URL state, tab activation, project selection)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV


def test_merged_state_prefers_url_then_local_storage_then_defaults():
    out = run_js(
        EV
        + """
const { g } = await setup({ app: true });
await g.flush();
const r = {};
['tq_last_tab','tq_project','tq_status','tq_queue_active','tq_tag','tq_last_doc'].forEach(k => g.localStorage.removeItem(k));
g.location.search = '';
r.defaults = g._mergedUiState();
g.localStorage.setItem('tq_last_tab', 'audit');
g.localStorage.setItem('tq_project', 'pp');
g.localStorage.setItem('tq_status', 'failed');
g.localStorage.setItem('tq_queue_active', '1');
g.localStorage.setItem('tq_tag', 'gpu');
g.localStorage.setItem('tq_last_doc', 'cli');
r.stored = g._mergedUiState();
g.localStorage.setItem('tq_last_tab', 'nope');
g.localStorage.setItem('tq_project', 'all');
r.badTab = [g._mergedUiState().tab, g._mergedUiState().project];
g.location.search = '?page=usage&project=px&status=running&q=hi&group=g1&tag=t1&doc=d&section=s';
r.url = g._mergedUiState();
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["defaults"] == {"tab": "queue", "project": "all", "status": "all", "active": False, "q": "",
                               "group": None, "tag": None, "doc": None, "section": None}
    assert out["stored"]["tab"] == "audit" and out["stored"]["project"] == "pp" and out["stored"]["status"] == "failed"
    assert out["stored"]["active"] is True and out["stored"]["tag"] == "gpu" and out["stored"]["doc"] == "cli"
    assert out["badTab"] == ["queue", "all"]
    assert out["url"] == {"tab": "usage", "project": "px", "status": "running", "active": True, "q": "hi",
                          "group": "g1", "tag": "t1", "doc": "d", "section": "s"}


def test_build_params_drop_token_and_docs_state_only_on_docs_tab():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true });
await g.flush();
const r = {};
g.location.search = '?tq_token=abc';
S.selectedProject = 'p1'; S.queueStatusFilter = 'failed'; S.groupFilter = 'g'; S.tagFilter = 't';
S.docsCurrentId = 'guide'; S.docsSectionId = 'sec';
doc.getElementById('queue-search').value = ' needle ';
g._activateTabFromUrl('queue');
r.queue = g._buildUiSearchParams().toString();
g._activateTabFromUrl('docs');
r.docs = g._buildUiSearchParams().toString();
S.applyingUrl = true;
const n = g.history.pushed.length + g.history.replaced.length;
g._syncUrl();
r.suppressed = g.history.pushed.length + g.history.replaced.length === n;
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["queue"] == "page=queue&project=p1&status=failed&q=needle&group=g&tag=t"
    assert out["docs"].endswith("&doc=guide&section=sec") and "page=docs" in out["docs"]
    assert out["suppressed"] is True


def test_activate_tab_switches_pages_nav_and_remembers_and_falls_back():
    out = run_js(
        EV
        + """
const { g, doc } = await setup({ app: true });
await g.flush();
const r = {};
r.name = g._activateTabFromUrl('audit');
const pages = () => doc.querySelectorAll('.page').filter(p => p.classList.has('active')).map(p => p.id);
const navs = () => doc.querySelectorAll('nav button').filter(b => b.classList.has('active')).map(b => b.getAttribute('data-tab'));
r.audit = [pages(), navs(), g.localStorage.getItem('tq_last_tab')];
r.fallback = [g._activateTabFromUrl('bogus'), pages()];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["name"] == "audit"
    assert out["audit"] == [["audit"], ["audit"], "audit"]
    assert out["fallback"] == ["queue", ["queue"]]


def test_apply_ui_vars_sets_filters_and_persists_without_fetching():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true });
await g.flush();
const r = {};
g.fetches.length = 0;
g._applyUiVars({ project: 'px', status: 'failed', q: 'zz', group: 'g1', tag: 't1', doc: 'd', section: 's', active: true });
r.state = [S.selectedProject, S.queueProjectFilter, S.queueStatusFilter, S.groupFilter, S.tagFilter, S.docsCurrentId, S.docsSectionId, S.queueActiveOnly];
r.search = doc.getElementById('queue-search').value;
r.stored = ['tq_project', 'tq_status', 'tq_tag', 'tq_last_doc'].map(k => g.localStorage.getItem(k));
r.fetched = g.fetches.map(f => f.url);
g._applyUiVars({ project: 'all', active: true });
r.all = [S.selectedProject, S.queueActiveOnly, S.groupFilter, S.queueStatusFilter];
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["state"] == ["px", "px", "failed", "g1", "t1", "d", "s", False]
    assert out["search"] == "zz" and out["stored"] == ["px", "failed", "t1", "d"] and not any(u.startswith("/api/status") for u in out["fetched"])
    assert out["all"] == ["all", True, None, "all"]


def test_set_project_resets_filters_refreshes_and_syncs_url_chips_and_waiters():
    out = run_js(
        EV
        + """
const { g, doc, S } = await setup({ app: true });
await g.flush();
const r = {};
S.groupFilter = 'old'; S.queuePage = 3; S.historyPage = 2; S.queueActiveOnly = true; S.queueNewFailedOnly = true;
g.fetches.length = 0;
g.setProject('px');
r.state = [S.selectedProject, S.groupFilter, S.queuePage, S.historyPage, S.queueActiveOnly, S.queueNewFailedOnly];
r.stored = [g.localStorage.getItem('tq_project'), g.localStorage.getItem('tq_queue_active')];
r.url = g.history.pushed[g.history.pushed.length - 1];
await g.flush();
r.fetched = g.fetches.map(f => f.url);
// keepGroup / keepActive options preserve their state
S.groupFilter = 'keep';
g.setProject('py', { keepGroup: true });
r.keptGroup = S.groupFilter;
g.setQueueActive();
r.active = [S.queueActiveOnly, S.selectedProject];
// blank value means all projects; the waiter resolves once the refresh finishes
let resolved = false;
g.setProjectAndWait('').then(() => { resolved = true; });
r.sel = S.selectedProject;
await g.flush(); await g.flush();
console.log('RESULT:' + JSON.stringify(r));
"""
    )
    assert out["state"] == ["px", None, 0, 0, False, False]
    assert out["stored"] == ["px", "0"] and "project=px" in out["url"]
    assert out["fetched"], out["fetched"]
    assert out["keptGroup"] == "keep"
    assert out["active"] == [True, "all"] and out["sel"] == "all"
