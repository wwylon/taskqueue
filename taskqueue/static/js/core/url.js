// Shareable UI URL state (page/project/status/q/group/tag/doc), tab activation, and unified project selection.
// Uses app.js globals at call time (refresh, loadDeps, loadGroups, tqBusy, _updateFreezeBtn, _showProjectRoot, ...).
import { S } from '../core/state.js';

export const UI_TABS = ['queue','dashboard','history','workers','groups','deps','resources','usage','tasks','timeline','schedule','alerts','audit','api','docs'];

export function _lsGet(k) {
  try { return localStorage.getItem(k); } catch (e) { return null; }
}

/** Parse shareable UI query params. page= is canonical; tab= is an older alias. */
export function _readUiUrl(search) {
  const sp = new URLSearchParams(
    search != null ? String(search).replace(/^\?/, '') : location.search
  );
  const out = {};
  const page = sp.get('page') || sp.get('tab');
  if (page && UI_TABS.includes(page)) out.tab = page;
  const project = sp.get('project');
  if (project) out.project = project;
  const status = sp.get('status');
  if (status) out.status = status;
  if (sp.has('q')) out.q = sp.get('q') || '';
  const group = sp.get('group');
  if (group) out.group = group;
  const tag = sp.get('tag');
  if (tag) out.tag = tag;
  const doc = sp.get('doc');
  if (doc) out.doc = doc;
  const section = sp.get('section');
  if (section) out.section = section;
  return out;
}

export function _mergedUiState() {
  const fromUrl = _readUiUrl();
  const lastTab = _lsGet('tq_last_tab');
  const tag = fromUrl.tag || _lsGet('tq_tag') || null;
  return {
    tab: fromUrl.tab || ((lastTab && UI_TABS.includes(lastTab)) ? lastTab : 'queue'),
    project: fromUrl.project || ((_lsGet('tq_project') && _lsGet('tq_project') !== 'all') ? _lsGet('tq_project') : 'all'),
    status: fromUrl.status || ((_lsGet('tq_status') && _lsGet('tq_status') !== 'all') ? _lsGet('tq_status') : 'all'),
    active: _lsGet('tq_queue_active') === '1',
    q: Object.prototype.hasOwnProperty.call(fromUrl, 'q') ? fromUrl.q : '',
    group: fromUrl.group || null,
    tag: tag || null,
    doc: fromUrl.doc || _lsGet('tq_last_doc') || null,
    section: fromUrl.section || null,
  };
}

/** Build query string for the current UI. Always includes page=. */
export function _buildUiSearchParams() {
  const sp = new URLSearchParams();
  let tabName = _activePageTab();
  if (!tabName || !UI_TABS.includes(tabName)) tabName = 'queue';
  sp.set('page', tabName);
  if (S.selectedProject && S.selectedProject !== 'all') sp.set('project', S.selectedProject);
  if (S.queueStatusFilter && S.queueStatusFilter !== 'all') sp.set('status', S.queueStatusFilter);
  const q = ((document.getElementById('queue-search') || {}).value || '').trim();
  if (q) sp.set('q', q);
  if (S.groupFilter) sp.set('group', S.groupFilter);
  if (S.tagFilter) sp.set('tag', S.tagFilter);
  if (tabName === 'docs' && S.docsCurrentId) sp.set('doc', S.docsCurrentId);
  if (tabName === 'docs' && S.docsSectionId) sp.set('section', S.docsSectionId);
  return sp;
}

export function _syncUrl(opts) {
  opts = opts || {};
  if (S.applyingUrl) return;
  const next = _buildUiSearchParams().toString();
  const cur = location.search.replace(/^\?/, '');
  if (next === cur) return;
  const url = next ? ('?' + next) : location.pathname;
  if (opts.replace) history.replaceState({ tqUi: 1 }, '', url);
  else history.pushState({ tqUi: 1 }, '', url);
}

export function _activateTabFromUrl(name) {
  if (!UI_TABS.includes(name)) name = 'queue';
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  const page = document.getElementById(name);
  if (page) page.classList.add('active');
  const btn = document.querySelector('nav button[data-tab="' + name + '"]');
  if (btn) btn.classList.add('active');
  try { localStorage.setItem('tq_last_tab', name); } catch (_) {}
  if (name !== 'queue') tqCloseShortcuts();
  tqSyncHeaderMark();
  return name;
}

/** Apply project, status, search, and related filters without fetching. */
export function _applyUiVars(state) {
  state = state || {};
  const proj = (state.project && state.project !== 'all') ? state.project : 'all';
  S.selectedProject = proj;
  S.queueProjectFilter = proj;
  if (state.active !== undefined) S.queueActiveOnly = !!state.active && proj === 'all';
  _syncProjSelect();
  const isSpecific = proj !== 'all';
  const fb = document.getElementById('proj-freeze-btn');
  const sb = document.getElementById('proj-stopall-btn');
  const pb = document.getElementById('proj-pause-btn');
  if (pb) pb.style.display = isSpecific ? '' : 'none';
  if (fb) fb.style.display = isSpecific ? '' : 'none';
  if (sb) sb.style.display = isSpecific ? '' : 'none';
  _updateFreezeBtn();
  _showProjectRoot(proj);
  _syncProjBarChips();
  S.queueStatusFilter = state.status || 'all';
  S.savedToday = false;
  tqSyncStatusChips();
  S.groupFilter = state.group || null;
  S.tagFilter = state.tag || null;
  if (S.tagFilter) {
    const tagBar = document.getElementById('queue-tag-bar');
    if (tagBar) tagBar.dataset.expanded = '1';
  }
  if (S.groupFilter) {
    const groupBar = document.getElementById('queue-group-bar');
    if (groupBar) groupBar.dataset.expanded = '1';
  }
  _updateTagChips();
  const inp = document.getElementById('queue-search');
  if (inp) inp.value = state.q != null ? state.q : '';
  _syncQueueSearchClear();
  S.docsCurrentId = state.doc || null;
  S.docsSectionId = state.section || null;
  try { localStorage.setItem('tq_project', S.selectedProject); } catch (e) {}
  try { localStorage.setItem('tq_status', S.queueStatusFilter); } catch (e) {}
  try { localStorage.setItem('tq_tag', S.tagFilter || ''); } catch (e) {}
  if (S.docsCurrentId) {
    try { localStorage.setItem('tq_last_doc', S.docsCurrentId); } catch (e) {}
  }
}



/** Clear search without refreshing — used when status/project/group/tag filters change. */

// D1: setProject — unified project selection
let _setProjectFetchResolvers = [];
export function setProject(val, opts) {
  opts = opts || {};
  const _newProject = val === '' ? 'all' : val;
  // Groups belong to a project, so switching project drops the group filter.
  if (_newProject !== S.selectedProject && S.groupFilter && !opts.keepGroup) {
    S.groupFilter = null;
    _updateGroupChips();
  }
  S.selectedProject = _newProject;
  // Only the "active" chip keeps the active view; any other project choice leaves it.
  if (!opts.keepActive) S.queueActiveOnly = false;
  // A badge jump narrows failures to "new"; picking a project by hand drops that narrowing.
  if (!opts.keepActive) S.queueNewFailedOnly = false;
  // Match setQueueProject / other filters: a new project filter invalidates the page index.
  S.queuePage = 0;
  S.historyPage = 0;
  if (!opts.keepSearch) _resetQueueSearchForFilter();
  _syncProjSelect();
  try { localStorage.setItem('tq_project', S.selectedProject); } catch(e) {}
  try { localStorage.setItem('tq_queue_active', S.queueActiveOnly ? '1' : '0'); } catch(e) {}
  // W9.5: show/hide project action buttons
  const isSpecific = S.selectedProject !== 'all';
  const fb = document.getElementById('proj-freeze-btn');
  const sb = document.getElementById('proj-stopall-btn');
  const pb = document.getElementById('proj-pause-btn');
  if (pb) pb.style.display = isSpecific ? '' : 'none';
  if (fb) fb.style.display = isSpecific ? '' : 'none';
  if (sb) sb.style.display = isSpecific ? '' : 'none';
  _updateFreezeBtn();
  _showProjectRoot(S.selectedProject);
  S.historyLimit = S.historyPageSize;
  if (S.status) _updatePageTitle(S.status);
  else _updatePageTitle({});
  // Sync deps "all projects" checkbox with the global project filter
  const depsAllEl = document.getElementById('deps-all-projects');
  if (depsAllEl) depsAllEl.checked = (S.selectedProject === 'all');
  tqBusy(Promise.all([refresh(), loadDeps(), loadGroups()]));
  // The timeline is fetched per project, so reload it now instead of waiting for the periodic tick.
  const _activePage = document.querySelector('.page.active');
  if (_activePage && _activePage.id === 'timeline') loadTimeline();
  // D3: update queue chip active state
  _syncProjBarChips();
  // Sync dependent filters
  S.queueProjectFilter = S.selectedProject;
  const resolvers = _setProjectFetchResolvers.splice(0);
  resolvers.forEach(fn => fn());
  if (!S.applyingUrl) _syncUrl({ replace: false });
}

/** Mark the project chips in the queue and History bars: "all" and "active" are mutually exclusive views. */
export function _syncProjBarChips() {
  document.querySelectorAll('#queue-proj-bar .filter-chip, #history-proj-bar .filter-chip').forEach(b => {
    const on = b.dataset.queueView === 'active' || b.dataset.historyView === 'active'
      ? S.queueActiveOnly
      : (!S.queueActiveOnly && (b.dataset.project || 'all') === S.selectedProject);
    b.classList.toggle('active', on);
  });
}

/** Header project select: "" = all, "active" = the active view, otherwise a project name. */
export function _syncProjSelect() {
  const sel = document.getElementById('project-select');
  if (!sel) return;
  sel.value = S.queueActiveOnly ? 'active' : (S.selectedProject === 'all' ? '' : S.selectedProject);
}

// B4: setProjectAndWait — returns a Promise resolving after the next refresh completes
export function setProjectAndWait(project, opts) {
  setProject(project, opts);
  return new Promise(resolve => { S.refreshResolvers.push(resolve); });
}
