// @ts-check
// Shareable UI URL state (page/project/status/q/group/tag/doc), tab activation, and unified project selection.
// Uses app.js globals at call time (refresh, loadDeps, loadGroups, tqBusy, _updateFreezeBtn, _showProjectRoot, ...).
import { S } from '../core/state.js';
import { inputValue } from '../util/format.js';

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
  const projects = sp.get('projects');
  if (projects) {
    out.projects = projects.split(',').map(s => s.trim()).filter(Boolean);
  }
  const status = sp.get('status');
  if (status) out.status = status;
  if (sp.has('q')) out.q = sp.get('q') || '';
  const group = sp.get('group');
  if (group) out.group = group;
  const set = sp.get('set');
  if (set) out.set = set;
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
    projects: Array.isArray(fromUrl.projects) ? fromUrl.projects : null,
    status: fromUrl.status || ((_lsGet('tq_status') && _lsGet('tq_status') !== 'all') ? _lsGet('tq_status') : 'all'),
    active: _lsGet('tq_queue_active') === '1',
    q: Object.prototype.hasOwnProperty.call(fromUrl, 'q') ? fromUrl.q : '',
    group: fromUrl.group || null,
    set: fromUrl.set || null,
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
  if (Array.isArray(S.selectedProjects) && S.selectedProjects.length > 1) {
    sp.set('projects', S.selectedProjects.join(','));
  } else if (Array.isArray(S.selectedProjects) && S.selectedProjects.length === 1) {
    sp.set('project', S.selectedProjects[0]);
  } else if (S.selectedProject && S.selectedProject !== 'all') {
    sp.set('project', S.selectedProject);
  }
  if (S.queueStatusFilter && S.queueStatusFilter !== 'all') sp.set('status', S.queueStatusFilter);
  const q = (inputValue('queue-search') || '').trim();
  if (q) sp.set('q', q);
  if (S.groupFilter) sp.set('group', S.groupFilter);
  if (S.setFilter) sp.set('set', S.setFilter);
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
  if (Array.isArray(state.projects) && state.projects.length) {
    S.selectedProjects = state.projects.slice();
    S.selectedProject = state.projects.length === 1 ? state.projects[0] : 'all';
  } else {
    const proj = (state.project && state.project !== 'all') ? state.project : 'all';
    S.selectedProject = proj;
    S.selectedProjects = proj === 'all' ? [] : [proj];
  }
  S.queueProjectFilter = S.selectedProject;
  if (state.active !== undefined) S.queueActiveOnly = !!state.active && S.selectedProject === 'all' && !(S.selectedProjects || []).length;
  _syncProjSelect();
  const multi = Array.isArray(S.selectedProjects) && S.selectedProjects.length > 1;
  const isSpecific = !multi && S.selectedProject !== 'all';
  const fb = document.getElementById('proj-freeze-btn');
  const sb = document.getElementById('proj-stopall-btn');
  const pb = document.getElementById('proj-pause-btn');
  if (pb) pb.style.display = isSpecific ? '' : 'none';
  if (fb) fb.style.display = isSpecific ? '' : 'none';
  if (sb) sb.style.display = isSpecific ? '' : 'none';
  if (typeof _updateFreezeBtn === 'function') _updateFreezeBtn();
  if (typeof _showProjectRoot === 'function') _showProjectRoot(S.selectedProject);
  _syncProjBarChips();
  S.queueStatusFilter = state.status || 'all';
  S.savedToday = false;
  tqSyncStatusChips();
  S.groupFilter = state.group || null;
  S.setFilter = state.set || null;
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
  if (typeof _populateSetBar === 'function') _populateSetBar();
  const inp = /** @type {HTMLInputElement | null} */ (document.getElementById('queue-search'));
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

let _setProjectFetchResolvers = [];
export function setProject(val, opts) {
  opts = opts || {};
  const _newProject = val === '' ? 'all' : val;
  if (_newProject !== S.selectedProject && S.groupFilter && !opts.keepGroup) {
    S.groupFilter = null;
    _updateGroupChips();
  }
  S.selectedProject = _newProject;
  S.selectedProjects = _newProject === 'all' ? [] : [_newProject];
  if (!opts.keepActive) S.queueActiveOnly = false;
  if (!opts.keepActive) S.queueNewFailedOnly = false;
  S.queuePage = 0;
  S.historyPage = 0;
  if (!opts.keepSearch) _resetQueueSearchForFilter();
  _syncProjSelect();
  try { localStorage.setItem('tq_project', S.selectedProject); } catch(e) {}
  try { localStorage.setItem('tq_queue_active', S.queueActiveOnly ? '1' : '0'); } catch(e) {}
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
  const depsAllEl = /** @type {HTMLInputElement | null} */ (document.getElementById('deps-all-projects'));
  if (depsAllEl) depsAllEl.checked = (S.selectedProject === 'all');
  tqBusy(Promise.all([refresh(), loadDeps(), loadGroups()]));
  const _activePage = document.querySelector('.page.active');
  if (_activePage && _activePage.id === 'timeline') loadTimeline();
  _syncProjBarChips();
  if (typeof _updateSetBulkBar === 'function') _updateSetBulkBar();
  S.queueProjectFilter = S.selectedProject;
  const resolvers = _setProjectFetchResolvers.splice(0);
  resolvers.forEach(fn => fn());
  if (!S.applyingUrl) _syncUrl({ replace: false });
}

export function toggleProjectChip(name) {
  if (!name || name === 'all') return setProject('all');
  const cur = Array.isArray(S.selectedProjects) ? S.selectedProjects.slice() : [];
  const i = cur.indexOf(name);
  if (i >= 0) cur.splice(i, 1);
  else cur.push(name);
  S.selectedProjects = cur;
  S.selectedProject = cur.length === 1 ? cur[0] : 'all';
  S.queueActiveOnly = false;
  S.queueNewFailedOnly = false;
  S.queuePage = 0;
  S.historyPage = 0;
  if (S.groupFilter && cur.length !== 1) {
    S.groupFilter = null;
    if (typeof _updateGroupChips === 'function') _updateGroupChips();
  }
  _resetQueueSearchForFilter();
  _syncProjSelect();
  try { localStorage.setItem('tq_project', S.selectedProject); } catch (e) {}
  const multi = cur.length > 1;
  const isSpecific = !multi && S.selectedProject !== 'all';
  const fb = document.getElementById('proj-freeze-btn');
  const sb = document.getElementById('proj-stopall-btn');
  const pb = document.getElementById('proj-pause-btn');
  if (pb) pb.style.display = isSpecific ? '' : 'none';
  if (fb) fb.style.display = isSpecific ? '' : 'none';
  if (sb) sb.style.display = isSpecific ? '' : 'none';
  if (typeof _updateFreezeBtn === 'function') _updateFreezeBtn();
  if (typeof _showProjectRoot === 'function') _showProjectRoot(S.selectedProject);
  S.historyLimit = S.historyPageSize;
  if (S.status) _updatePageTitle(S.status);
  else _updatePageTitle({});
  tqBusy(Promise.all([refresh(), loadDeps(), loadGroups()]));
  _syncProjBarChips();
  if (typeof _updateSetBulkBar === 'function') _updateSetBulkBar();
  S.queueProjectFilter = S.selectedProject;
  if (!S.applyingUrl) _syncUrl({ replace: false });
}

export function _syncProjBarChips() {
  const selected = new Set(
    (Array.isArray(S.selectedProjects) && S.selectedProjects.length)
      ? S.selectedProjects
      : (S.selectedProject && S.selectedProject !== 'all' ? [S.selectedProject] : [])
  );
  /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('#queue-proj-bar .filter-chip, #history-proj-bar .filter-chip')).forEach(b => {
    const on = b.dataset.queueView === 'active' || b.dataset.historyView === 'active'
      ? S.queueActiveOnly
      : (!S.queueActiveOnly && (
          (b.dataset.project === 'all' && selected.size === 0)
          || (b.dataset.project && selected.has(b.dataset.project))
        ));
    b.classList.toggle('active', on);
  });
}

export function _syncProjSelect() {
  const sel = /** @type {HTMLSelectElement | null} */ (document.getElementById('project-select'));
  if (!sel) return;
  if (Array.isArray(S.selectedProjects) && S.selectedProjects.length > 1) {
    sel.value = '';
    return;
  }
  if (!S.queueActiveOnly && S.selectedProject === 'all' && S.setFilter && S.setFilter !== '__unset__') {
    sel.value = 'set:' + S.setFilter;
    if (sel.value) return;
  }
  sel.value = S.queueActiveOnly ? 'active' : (S.selectedProject === 'all' ? '' : S.selectedProject);
}

export function setProjectAndWait(project, opts) {
  setProject(project, opts);
  return new Promise(resolve => { S.refreshResolvers.push(resolve); });
}
