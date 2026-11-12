// @ts-check
// Filter bars: header project/worker selects, queue/history project chips, tag chips and the broken-deps banner.
// Uses app.js globals at call time (tqAct, i18n, showToast, setProject, setQueueActive, refresh, renderQueue, ...).
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';

export function _populateProjectSelect(projects) {
  const sel = document.getElementById('project-select');
  // D1: validate stored project still exists; reset silently if not.
  if (S.selectedProject !== 'all' && !projects.includes(S.selectedProject)) {
    S.selectedProject = 'all';
    S.queueProjectFilter = 'all';
    try { localStorage.setItem('tq_project', 'all'); } catch(e) {}
    if (!S.applyingUrl) _syncUrl({ replace: true });
  }
  sel.innerHTML = '<option value="">all</option>' +
    '<option value="active" data-i18n="filter.active">active</option>' +
    _setOptionsHtml() +
    `<optgroup label="${escHtml(i18n.t('filter.projectsGroup'))}">` +
    projects.map(p => `<option value="${escHtml(p)}">${escHtml(p)}</option>`).join('') +
    '</optgroup>';
  _syncProjSelect();
}

/** Header picker: project-set tree as indented `set:<path>` options (all > bots > bridge2 …). */
function _setOptionsHtml() {
  const flat = (S.projectSets && S.projectSets.flat) || {};
  const paths = Object.keys(flat).sort();
  if (!paths.length) return '';
  const opts = paths.map(p => {
    const depth = p.split('/').length - 1;
    const name = p.split('/').pop();
    const n = (flat[p] && flat[p].subtree_count) || 0;
    return `<option value="set:${escHtml(p)}">${'  '.repeat(depth)}▸ ${escHtml(name)} (${n})</option>`;
  });
  return `<optgroup label="${escHtml(i18n.t('filter.setsGroup'))}">${opts.join('')}</optgroup>`;
}

export function _populateWorkerSelect(workers) {
  const sel = /** @type {HTMLInputElement | null} */ (document.getElementById('worker-select'));
  if (!sel) return;
  const prev = sel.value;
  const ids = (Array.isArray(workers) ? workers : []).map(w => w.worker_id);
  sel.innerHTML = '<option value="">all</option>' +
    `<option value="master"${prev === 'master' ? ' selected' : ''}>master</option>` +
    ids.map(id => `<option value="${escHtml(id)}"${id===prev?' selected':''}>${escHtml(id)}</option>`).join('');
}

export function onProjectChange() {
  // The header select also offers the "active" view, which is not a project.
  const v = /** @type {HTMLInputElement} */ (document.getElementById('project-select')).value;
  if (v === 'active') return setQueueActive();
  if (v.startsWith('set:')) {
    // A set node selects its whole subtree; the queue/project chips follow the set filter.
    setProject('all', { keepGroup: true });
    return setProjectSet(v.slice(4));
  }
  setProject(v || 'all');
  if (!v && S.setFilter) setProjectSet('');  // "all" means all projects, not "all in the set"
}

export function onWorkerChange() {
  S.globalWorkerFilter = /** @type {HTMLInputElement} */ (document.getElementById('worker-select')).value;
  // F-258: server-side ?worker= — reset pages and refetch.
  S.queuePage = 0;
  S.historyOffset = 0;
  S.historyPage = 0;
  if (typeof window.refresh === 'function') window.refresh();
  else {
    renderQueue();
    renderHistory();
    renderDashboard();
  }
}

export function _populateProjectChips(projects) {
  let visibleProjects = projects;
  if (typeof projectsInSetFilter === 'function') {
    visibleProjects = projectsInSetFilter(visibleProjects);
  }
  if (S.tagFilter && S.allTagProjects[S.tagFilter]) {
    const taggedSet = new Set(S.allTagProjects[S.tagFilter]);
    visibleProjects = visibleProjects.filter(p => taggedSet.has(p));
  }
  if (S.mineOnly && S.operator && Array.isArray(S.myProjects)) {
    const mine = new Set(S.myProjects);
    visibleProjects = visibleProjects.filter(p => mine.has(p));
  }
  const mineChip = (S.operator)
    ? `<button class="filter-chip${S.mineOnly ? ' active' : ''}" data-scope="mine" data-click="${tqAct([['tqSetMineScope', true]])}" title="Tasks you started" data-i18n="filter.mine" data-i18n-title="filter.mineTitle">mine</button>` +
      `<button class="filter-chip${!S.mineOnly ? ' active' : ''}" data-scope="all" data-click="${tqAct([['tqSetMineScope', false]])}" title="All operators" data-i18n="filter.viewAll" data-i18n-title="filter.viewAllTitle">all ops</button>`
    : '';
  const selected = new Set(
    (Array.isArray(S.selectedProjects) && S.selectedProjects.length)
      ? S.selectedProjects
      : (S.selectedProject && S.selectedProject !== 'all' ? [S.selectedProject] : [])
  );
  const allActive = selected.size === 0 && !S.queueActiveOnly;
  const qbar = document.getElementById('queue-proj-bar');
  if (qbar) {
    qbar.innerHTML = `<span class="filter-label">${i18n.t('filter.labelProject')}</span>` + mineChip +
      `<button class="filter-chip${allActive ? ' active' : ''}" data-project="all" data-click="${tqAct([['setProject', 'all']])}">${i18n.t('filter.all')}</button>` +
      `<button class="filter-chip${S.queueActiveOnly?' active':''}" data-queue-view="active" data-click="${tqAct([['setQueueActive']])}" title="${escHtml(i18n.t('filter.activeTitle'))}" data-i18n="filter.active" data-i18n-title="filter.activeTitle">${i18n.t('filter.active')}</button>` +
      visibleProjects.map(p => {
        const froz = !!(S.cfg && S.cfg[`project_frozen_${p}`]);
        const on = selected.has(p);
        return `<button class="filter-chip${on?' active':''}${froz?' chip-frozen':''}${_projStateClass(p)}" data-project="${escHtml(p)}" data-click="${tqAct([['toggleProjectChip', String(p)]])}">${on?'☑ ':'☐ '}${froz?'❄ ':''}${escHtml(p)}</button>`;
      }).join('');
  }
  const hbar = document.getElementById('history-proj-bar');
  if (hbar) {
    let histProjects = (typeof projectsInSetFilter === 'function')
      ? projectsInSetFilter(projects)
      : projects;
    if (S.mineOnly && S.operator && Array.isArray(S.myProjects)) {
      histProjects = histProjects.filter(p => S.myProjects.includes(p));
    }
    hbar.innerHTML = `<span class="filter-label">${i18n.t('filter.labelProject')}</span>` + mineChip +
      `<button class="filter-chip${allActive ? ' active' : ''}" data-project="all" data-click="${tqAct([['setProject', 'all']])}">${i18n.t('filter.all')}</button>` +
      `<button class="filter-chip${S.queueActiveOnly?' active':''}" data-history-view="active" data-click="${tqAct([['setQueueActive']])}" title="${escHtml(i18n.t('filter.activeTitle'))}" data-i18n="filter.active" data-i18n-title="filter.activeTitle">${i18n.t('filter.active')}</button>` +
      histProjects.map(p => {
        const on = selected.has(p);
        return `<button class="filter-chip${on?' active':''}" data-project="${escHtml(p)}" data-click="${tqAct([['toggleProjectChip', String(p)]])}">${on?'☑ ':'☐ '}${escHtml(p)}</button>`;
      }).join('');
  }
  if (typeof _updateSetBulkBar === 'function') _updateSetBulkBar();
}

const _PROJECT_STATES = ['failed', 'interrupted', 'running', 'waiting', 'completed'];

/** Queue project chip colour = the project's worst live state (API ``project_states``). */
function _projStateClass(p) {
  const st = ((S.status && S.status.project_states) || {})[p];
  return _PROJECT_STATES.includes(st) ? ` chip-st chip-st-${st}` : '';
}

/** Re-colour existing chips on each status refresh without rebuilding the bar. */
export function _updateProjectChipStates() {
  const states = (S.status && S.status.project_states) || {};
  document.querySelectorAll('#queue-proj-bar [data-project]').forEach(el => {
    const p = el.getAttribute('data-project');
    for (const st of _PROJECT_STATES) el.classList.remove('chip-st-' + st);
    const st = states[p];
    if (_PROJECT_STATES.includes(st)) el.classList.add('chip-st', 'chip-st-' + st);
    else el.classList.remove('chip-st');
  });
}

/** Groups/tags only make sense once a scope is picked (project, set or selection), not for "all". */
export function isAllProjectsScope() {
  return S.selectedProject === 'all' && !(Array.isArray(S.selectedProjects) && S.selectedProjects.length) && !S.setFilter;
}

export const TAG_CHIP_CAP = 60;

export function _updateTagChips() {
  const bar = document.getElementById('queue-tag-bar');
  if (!bar) return;
  if (isAllProjectsScope()) {
    S.tagFilter = null;  // a hidden filter must not keep narrowing the queue
    bar.style.display = 'none';
    return;
  }
  if (!S.allTags.length) { bar.style.display = 'none'; return; }
  const expanded = bar.dataset.expanded === '1';
  // A few thousand tasks can carry hundreds of tags: cap the chips (the active tag always stays visible).
  const shown = S.allTags.slice(0, TAG_CHIP_CAP);
  if (S.tagFilter && !shown.includes(S.tagFilter) && S.allTags.includes(S.tagFilter)) shown.push(S.tagFilter);
  const chips = shown.map(tag => {
    const active = S.tagFilter === tag;
    return `<button class="filter-chip${active ? ' active' : ''}" data-click="${tqAct([['toggleTagFilter', String(tag)]])}">${escHtml(tag)}</button>`;
  }).join('') + (S.allTags.length > shown.length
    ? `<span class="filter-label">${i18n.t('filter.tagsMore', {n: S.allTags.length - shown.length})}</span>` : '');
  bar.innerHTML = `<span class="filter-label" data-click="${tqAct([['_toggleTagBar']])}" style="cursor:pointer;user-select:none">${i18n.t('filter.tagsLabel', {arrow: expanded ? '▴' : '▾'})}</span>`
    + (expanded ? chips : '');
  bar.style.display = '';
}

export function _toggleTagBar() {
  const bar = document.getElementById('queue-tag-bar');
  if (!bar) return;
  bar.dataset.expanded = bar.dataset.expanded === '1' ? '0' : '1';
  _updateTagChips();
}

export function toggleTagFilter(tag) {
  S.tagFilter = (S.tagFilter === tag) ? null : tag;
  S.queuePage = 0;
  _resetQueueSearchForFilter();
  try { localStorage.setItem('tq_tag', S.tagFilter || ''); } catch(e) {}
  _updateTagChips();
  if (S.projects && S.projects.length) _populateProjectChips(S.projects);
  refresh();
  if (!S.applyingUrl) _syncUrl({ replace: false });
}





export function _updateBrokenDepsBanner() {
  const el = document.getElementById('broken-deps-banner');
  if (!el) return;
  const items = Array.isArray(S.brokenDeps) ? S.brokenDeps : [];
  const n = items.length;
  if (!n) {
    el.style.display = 'none';
    S.brokenDepsBannerDismissedCount = null;
    return;
  }
  if (S.brokenDepsBannerDismissedCount === n) {
    el.style.display = 'none';
    return;
  }
  const names = items.slice(0, 5).map(b => `${b.project}/${b.task_id}`).join(', ');
  const more = n > 5 ? i18n.t('banner.brokenDepsMore', {n: n - 5}) : '';
  el.innerHTML = `<span>${i18n.t('banner.brokenDeps', {n})} — ${escHtml(names)}${more ? ' ' + escHtml(more) : ''}</span>`
    + `<button type="button" class="action-btn" style="margin-left:auto" data-click="${tqAct([['dismissBrokenDepsBanner']])}">${i18n.t('banner.dismiss')}</button>`;
  el.style.display = 'flex';
  try {
    if (!sessionStorage.getItem('tq_broken_deps_toasted')) {
      sessionStorage.setItem('tq_broken_deps_toasted', '1');
      showToast(i18n.t('toast.brokenDeps', {n}), 'err');
    }
  } catch (e) {
    showToast(i18n.t('toast.brokenDeps', {n}), 'err');
  }
}

export function dismissBrokenDepsBanner() {
  S.brokenDepsBannerDismissedCount = Array.isArray(S.brokenDeps) ? S.brokenDeps.length : 0;
  const el = document.getElementById('broken-deps-banner');
  if (el) el.style.display = 'none';
}
