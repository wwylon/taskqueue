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
    projects.map(p => `<option value="${escHtml(p)}">${escHtml(p)}</option>`).join('');
  _syncProjSelect();
}

export function _populateWorkerSelect(workers) {
  const sel = document.getElementById('worker-select');
  if (!sel) return;
  const prev = sel.value;
  const ids = (Array.isArray(workers) ? workers : []).map(w => w.worker_id);
  sel.innerHTML = '<option value="">all</option>' +
    `<option value="master"${prev === 'master' ? ' selected' : ''}>master</option>` +
    ids.map(id => `<option value="${escHtml(id)}"${id===prev?' selected':''}>${escHtml(id)}</option>`).join('');
}

export function onProjectChange() {
  // The header select also offers the "active" view, which is not a project.
  const v = document.getElementById('project-select').value;
  if (v === 'active') return setQueueActive();
  setProject(v || 'all');
}

export function onWorkerChange() {
  S.globalWorkerFilter = document.getElementById('worker-select').value;
  renderQueue();
  renderHistory();
  renderDashboard();
}

export function _populateProjectChips(projects) {
  // When a tag is selected, only show projects that have tasks with that tag
  let visibleProjects = projects;
  if (S.tagFilter && S.allTagProjects[S.tagFilter]) {
    const taggedSet = new Set(S.allTagProjects[S.tagFilter]);
    visibleProjects = projects.filter(p => taggedSet.has(p));
  }
  // R8: mine scope narrows chips to projects where this operator has tasks.
  if (S.mineOnly && S.operator && Array.isArray(S.myProjects)) {
    const mine = new Set(S.myProjects);
    visibleProjects = visibleProjects.filter(p => mine.has(p));
  }
  const mineChip = (S.operator)
    ? `<button class="filter-chip${S.mineOnly ? ' active' : ''}" data-scope="mine" data-click="${tqAct([['tqSetMineScope', true]])}" title="Tasks you started" data-i18n="filter.mine" data-i18n-title="filter.mineTitle">mine</button>` +
      `<button class="filter-chip${!S.mineOnly ? ' active' : ''}" data-scope="all" data-click="${tqAct([['tqSetMineScope', false]])}" title="All operators" data-i18n="filter.viewAll" data-i18n-title="filter.viewAllTitle">all ops</button>`
    : '';
  // Queue project bar: D3 — chips call setProject
  const qbar = document.getElementById('queue-proj-bar');
  if (qbar) {
    const cur = S.selectedProject;
    qbar.innerHTML = `<span class="filter-label">project:</span>` + mineChip +
      `<button class="filter-chip${cur==='all'&&!S.queueActiveOnly?' active':''}" data-project="all" data-click="${tqAct([['setProject', 'all']])}">all</button>` +
      `<button class="filter-chip${S.queueActiveOnly?' active':''}" data-queue-view="active" data-click="${tqAct([['setQueueActive']])}" title="All projects except frozen/paused" data-i18n="filter.active" data-i18n-title="filter.activeTitle">active</button>` +
      visibleProjects.map(p => {
        const froz = !!(S.cfg && S.cfg[`project_frozen_${p}`]);
        return `<button class="filter-chip${cur===p?' active':''}${froz?' chip-frozen':''}" data-project="${escHtml(p)}" data-click="${tqAct([['setProject', String(p)]])}">${froz?'❄ ':''}${escHtml(p)}</button>`;
      }).join('');
  }
  // History project bar stays independent (no tag filter applied)
  const hbar = document.getElementById('history-proj-bar');
  if (hbar) {
    const cur = S.selectedProject;
    const histProjects = (S.mineOnly && S.operator && Array.isArray(S.myProjects))
      ? projects.filter(p => S.myProjects.includes(p))
      : projects;
    hbar.innerHTML = `<span class="filter-label">project:</span>` + mineChip +
      `<button class="filter-chip${cur==='all'&&!S.queueActiveOnly?' active':''}" data-project="all" data-click="${tqAct([['setProject', 'all']])}">all</button>` +
      `<button class="filter-chip${S.queueActiveOnly?' active':''}" data-history-view="active" data-click="${tqAct([['setQueueActive']])}" title="All projects except frozen/paused" data-i18n="filter.active" data-i18n-title="filter.activeTitle">active</button>` +
      histProjects.map(p => `<button class="filter-chip${cur===p?' active':''}" data-project="${escHtml(p)}" data-click="${tqAct([['setProject', String(p)]])}">${escHtml(p)}</button>`).join('');
  }
}

export const TAG_CHIP_CAP = 60;

export function _updateTagChips() {
  const bar = document.getElementById('queue-tag-bar');
  if (!bar) return;
  if (!S.allTags.length) { bar.style.display = 'none'; return; }
  const expanded = bar.dataset.expanded === '1';
  // A few thousand tasks can carry hundreds of tags: cap the chips (the active tag always stays visible).
  const shown = S.allTags.slice(0, TAG_CHIP_CAP);
  if (S.tagFilter && !shown.includes(S.tagFilter) && S.allTags.includes(S.tagFilter)) shown.push(S.tagFilter);
  const chips = shown.map(tag => {
    const active = S.tagFilter === tag;
    return `<button class="filter-chip${active ? ' active' : ''}" data-click="${tqAct([['toggleTagFilter', String(tag)]])}">${escHtml(tag)}</button>`;
  }).join('') + (S.allTags.length > shown.length
    ? `<span class="filter-label">+${S.allTags.length - shown.length} more</span>` : '');
  bar.innerHTML = `<span class="filter-label" data-click="${tqAct([['_toggleTagBar']])}" style="cursor:pointer;user-select:none">tags${expanded ? ' ▴' : ' ▾'}:</span>`
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
