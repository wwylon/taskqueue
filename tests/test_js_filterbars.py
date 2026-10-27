"""Behavior tests for components/filterbars.js (project/worker selects, project and tag chips, broken-deps banner)."""
from __future__ import annotations

from tests.test_js_app import run_js
from tests.test_js_smoke import EV

PRE = """
const { g, doc, S } = await setup({ app: true, routes: {} });
await g.flush();
const toasts = []; g.showToast = (m, k) => toasts.push([m, k]);
const r = {};
const $ = id => doc.getElementById(id) || doc.add(id);
"""


def _js(body: str) -> str:
    return EV + PRE + body + "\nconsole.log('RESULT:' + JSON.stringify(r));\n"


def test_project_select_resets_missing_project_and_worker_select_keeps_choice():
    out = run_js(_js("""
$('project-select'); $('worker-select');
g._syncProjSelect = () => { r.synced = true; };
const urls = []; g._syncUrl = o => urls.push(o);
S.selectedProject = 'gone'; S.queueProjectFilter = 'gone'; S.applyingUrl = false;
g._populateProjectSelect(['a<b', 'c']);
const html = $('project-select').innerHTML;
r.sel = [S.selectedProject, S.queueProjectFilter, g.localStorage.getItem('tq_project'), urls.slice(), html.includes('a&lt;b'), html.includes('filter.active')];
S.selectedProject = 'c'; urls.length = 0;
g._populateProjectSelect(['a<b', 'c']);
r.kept = [S.selectedProject, urls.length];
$('worker-select').value = 'w2';
g._populateWorkerSelect([{ worker_id: 'w1' }, { worker_id: 'w2' }]);
const wh = $('worker-select').innerHTML;
r.workers = [wh.includes('value="master"'), wh.includes('value="w2" selected'), wh.includes('value="w1">')];
g._populateWorkerSelect(null);
r.noWorkers = !$('worker-select').innerHTML.includes('w1');
"""))
    assert out["sel"] == ["all", "all", "all", [{"replace": True}], True, True]
    assert out["kept"] == ["c", 0] and out["synced"] is True
    assert out["workers"] == [True, True, True] and out["noWorkers"] is True


def test_project_and_worker_change_handlers_dispatch():
    out = run_js(_js("""
const calls = [];
g.setQueueActive = () => calls.push('active'); g.setProject = p => calls.push('p:' + p);
g.renderQueue = () => calls.push('q'); g.renderHistory = () => calls.push('h'); g.renderDashboard = () => calls.push('d');
$('project-select').value = 'active'; g.onProjectChange();
$('project-select').value = 'x'; g.onProjectChange();
$('project-select').value = ''; g.onProjectChange();
$('worker-select').value = 'w1'; g.onWorkerChange();
r.calls = calls; r.worker = S.globalWorkerFilter;
"""))
    assert out["calls"] == ["active", "p:x", "p:all", "q", "h", "d"] and out["worker"] == "w1"


def test_project_chips_filter_by_tag_mark_active_and_frozen():
    out = run_js(_js("""
$('queue-proj-bar'); $('history-proj-bar');
S.selectedProject = 'b'; S.queueActiveOnly = false; S.cfg = { project_frozen_b: true };
S.tagFilter = 'gpu'; S.allTagProjects = { gpu: ['b'] };
g._populateProjectChips(['a', 'b']);
const q = $('queue-proj-bar').innerHTML, h = $('history-proj-bar').innerHTML;
r.queue = [q.includes('data-project="a"'), q.includes('chip-frozen'), q.includes('❄ b'), q.includes('filter-chip active chip-frozen" data-project="b"')];
r.history = [h.includes('data-project="a"'), h.includes('chip-frozen'), h.includes('data-project="b"')];
S.tagFilter = null; S.queueActiveOnly = true; g._populateProjectChips(['a', 'b']);
r.activeOnly = [$('queue-proj-bar').innerHTML.includes('active" data-queue-view="active"'), $('queue-proj-bar').innerHTML.includes('data-project="a"')];
"""))
    assert out["queue"] == [False, True, True, True]
    assert out["history"] == [True, False, True]
    assert out["activeOnly"] == [True, True]


def test_tag_chips_toggle_and_filter_persist():
    out = run_js(_js("""
const bar = $('queue-tag-bar');
S.allTags = [];
g._updateTagChips(); r.hidden = bar.style.display;
S.allTags = ['gpu', 'a<b'];
g._updateTagChips();
r.collapsed = [bar.style.display, bar.innerHTML.includes('filter-chip'), bar.innerHTML.includes('tags ▾')];
g._toggleTagBar();
r.expanded = [bar.dataset.expanded, bar.innerHTML.includes('a&lt;b'), bar.innerHTML.includes('tags ▴')];
let refreshed = 0; g.refresh = () => { refreshed++; };
g._resetQueueSearchForFilter = () => {}; const urls = []; g._syncUrl = o => urls.push(o);
let chips = 0; g._populateProjectChips = () => { chips++; };
S.projects = ['p']; S.applyingUrl = false; S.queuePage = 4;
g.toggleTagFilter('gpu');
r.on = [S.tagFilter, S.queuePage, g.localStorage.getItem('tq_tag'), refreshed, urls.slice(), bar.innerHTML.includes('filter-chip active')];
g.toggleTagFilter('gpu');
r.off = [S.tagFilter, g.localStorage.getItem('tq_tag')];
"""))
    assert out["hidden"] == "none" and out["collapsed"] == ["", False, True]
    assert out["expanded"] == ["1", True, True]
    assert out["on"] == ["gpu", 0, "gpu", 1, [{"replace": False}], True]
    assert out["off"] == [None, ""]


def test_broken_deps_banner_lists_dismisses_and_toasts_once():
    out = run_js(_js("""
const el = $('broken-deps-banner');
g.sessionStorage.removeItem('tq_broken_deps_toasted');
S.brokenDeps = []; S.brokenDepsBannerDismissedCount = 3;
g._updateBrokenDepsBanner();
r.none = [el.style.display, S.brokenDepsBannerDismissedCount];
S.brokenDeps = Array.from({ length: 7 }, (_, i) => ({ project: 'p', task_id: 't' + i }));
g._updateBrokenDepsBanner();
r.shown = [el.style.display, el.innerHTML.includes('p/t0'), el.innerHTML.includes('p/t4'), !el.innerHTML.includes('p/t5'), el.innerHTML.includes('banner.brokenDepsMore'), toasts.slice()];
g._updateBrokenDepsBanner();
r.once = toasts.length;
g.dismissBrokenDepsBanner();
r.dismissed = [el.style.display, S.brokenDepsBannerDismissedCount];
el.style.display = 'flex'; g._updateBrokenDepsBanner();
r.stayHidden = el.style.display;
S.brokenDeps.push({ project: 'p', task_id: 'new' }); g._updateBrokenDepsBanner();
r.reshown = el.style.display;
"""))
    assert out["none"] == ["none", None]
    assert out["shown"][:5] == ["flex", True, True, True, True] and out["shown"][5] == [["toast.brokenDeps", "err"]]
    assert out["once"] == 1
    assert out["dismissed"] == ["none", 7] and out["stayHidden"] == "none" and out["reshown"] == "flex"
