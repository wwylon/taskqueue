// Project-set tree filter (multilevel slash paths) — distinct from task YAML group:.
// Uses app.js globals at call time (tqAct, i18n, refresh, openConfirm, showToast, _syncUrl, ...).
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';

const UNSET = '__unset__';

/** Projects under the current set filter (null = no set filter). */
export function projectsInSetFilter(allProjects) {
  const list = Array.isArray(allProjects) ? allProjects : (S.projects || []);
  if (!S.setFilter) return list;
  if (S.setFilter === UNSET) {
    const map = (S.status && S.status.project_set_map) || S.projectSetMap || {};
    return list.filter(p => !map[p]);
  }
  const flat = (S.projectSets && S.projectSets.flat) || {};
  const node = flat[S.setFilter];
  if (node && Array.isArray(node.subtree_projects)) {
    const allow = new Set(node.subtree_projects);
    return list.filter(p => allow.has(p));
  }
  // Fallback: prefix match from project_set_map
  const map = (S.status && S.status.project_set_map) || S.projectSetMap || {};
  const prefix = S.setFilter;
  return list.filter(p => {
    const sp = map[p];
    return sp === prefix || (sp && sp.startsWith(prefix + '/'));
  });
}

function _treeNodeHtml(node, depth) {
  const path = node.path || '';
  const active = S.setFilter === path;
  const kids = node.children || [];
  const expanded = !S._setCollapsed || !S._setCollapsed[path];
  const toggle = kids.length
    ? `<button type="button" class="cfg-btn" style="padding:0 4px;font-size:10px" data-click="${tqAct([['tqToggleSetNode', path]])}">${expanded ? '▾' : '▸'}</button>`
    : `<span style="display:inline-block;width:14px"></span>`;
  const pad = depth * 12;
  let html = `<span style="display:inline-flex;align-items:center;gap:2px;margin-left:${pad}px">`
    + toggle
    + `<button class="filter-chip${active ? ' active' : ''}" data-set="${escHtml(path)}" data-click="${tqAct([['setProjectSet', path]])}">${escHtml(node.name || path)} (${node.subtree_count || 0})</button>`
    + `</span>`;
  if (expanded) {
    for (const c of kids) html += _treeNodeHtml(c, depth + 1);
  }
  return html;
}

export function _populateSetBar() {
  const bar = document.getElementById('queue-set-bar');
  if (!bar) return;
  const data = S.projectSets;
  const roots = (data && data.tree && data.tree.children) || [];
  const unset = (data && data.unset) || { projects: [], subtree_count: 0 };
  if (!roots.length && !(unset.subtree_count > 0)) {
    bar.style.display = 'none';
    bar.innerHTML = '';
    _updateSetBulkBar();
    return;
  }
  const allActive = !S.setFilter;
  const unsetActive = S.setFilter === UNSET;
  let html = `<span class="filter-label">${i18n.t('setfilter.setLabel')}</span>`
    + `<button class="filter-chip${allActive ? ' active' : ''}" data-set="" data-click="${tqAct([['setProjectSet', '']])}">all</button>`;
  for (const r of roots) html += _treeNodeHtml(r, 0);
  if (unset.subtree_count > 0) {
    html += `<button class="filter-chip${unsetActive ? ' active' : ''}" data-set="${UNSET}" data-click="${tqAct([['setProjectSet', UNSET]])}">${i18n.t('setfilter.unsetCount', {n: unset.subtree_count})}</button>`;
  }
  bar.innerHTML = html;
  bar.style.display = '';
  _updateSetBulkBar();
}

export function tqToggleSetNode(path) {
  if (!S._setCollapsed) S._setCollapsed = {};
  S._setCollapsed[path] = !S._setCollapsed[path];
  _populateSetBar();
}

export function setProjectSet(path) {
  const next = path ? String(path) : null;
  S.setFilter = next || null;
  S.queuePage = 0;
  S.historyPage = 0;
  S.historyOffset = 0;
  if (S.setFilter) {
    const allowed = new Set(projectsInSetFilter(S.projects));
    const cur = Array.isArray(S.selectedProjects) ? S.selectedProjects.filter(p => allowed.has(p)) : [];
    S.selectedProjects = cur;
    S.selectedProject = cur.length === 1 ? cur[0] : 'all';
    S.queueProjectFilter = S.selectedProject;
    try { localStorage.setItem('tq_project', S.selectedProject); } catch (e) {}
    if (typeof _syncProjSelect === 'function') _syncProjSelect();
  }
  if (typeof _resetQueueSearchForFilter === 'function') _resetQueueSearchForFilter();
  if (S.projects && S.projects.length && typeof _populateProjectChips === 'function') {
    _populateProjectChips(S.projects);
  }
  _populateSetBar();
  if (typeof refresh === 'function') refresh();
  if (!S.applyingUrl && typeof _syncUrl === 'function') _syncUrl({ replace: false });
}

function _bulkTargetProjects() {
  if (Array.isArray(S.selectedProjects) && S.selectedProjects.length) {
    return S.selectedProjects.slice();
  }
  if (S.setFilter && S.setFilter !== UNSET) {
    return projectsInSetFilter(S.projects);
  }
  return [];
}

export function _updateSetBulkBar() {
  const bar = document.getElementById('queue-set-bulk');
  if (!bar) return;
  const multi = Array.isArray(S.selectedProjects) && S.selectedProjects.length > 0;
  const setScope = !!(S.setFilter && S.setFilter !== UNSET);
  const show = multi || setScope;
  if (!show) {
    bar.hidden = true;
    bar.innerHTML = '';
    return;
  }
  const targets = _bulkTargetProjects();
  const n = targets.length;
  const label = multi ? `projects (${n})` : `set ${S.setFilter} (${n})`;
  bar.hidden = false;
  bar.innerHTML = `<span class="filter-label">${escHtml(label)}:</span>`
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSetBulk', 'freeze']])}">${i18n.t('setfilter.freeze')}</button>`
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSetBulk', 'unfreeze']])}">${i18n.t('setfilter.unfreeze')}</button>`
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSetBulk', 'pause']])}">Pause</button>`
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSetBulk', 'resume']])}">${i18n.t('setfilter.resume')}</button>`
    + `<button type="button" class="cfg-btn" style="color:var(--fail)" data-click="${tqAct([['tqSetBulk', 'stop-all']])}">${i18n.t('setfilter.stopAll')}</button>`;
}

export function tqSetBulk(action) {
  const multi = Array.isArray(S.selectedProjects) && S.selectedProjects.length > 0;
  const targets = _bulkTargetProjects();
  if (!targets.length) return;
  const n = targets.length;
  const label = action === 'stop-all' ? 'Stop all running tasks in' : action;
  const scope = multi ? `${n} selected project(s)` : `${n} project(s) in set "${S.setFilter}"`;
  openConfirm(`${label} ${scope}?`, async () => {
    try {
      let r;
      if (multi) {
        r = await fetch('/api/projects/bulk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ projects: targets, action }),
        });
      } else {
        r = await fetch(`/api/project-set/${action}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ set: S.setFilter }),
        });
      }
      const data = await r.json().catch(() => ({}));
      if (!r.ok || data.ok === false) {
        showToast(data.error || `HTTP ${r.status}`, 'err');
        return;
      }
      showToast(`${action}: ${(data.projects || targets).length} project(s)`, 'ok');
      if (typeof refresh === 'function') refresh();
    } catch (e) {
      showToast(String(e && e.message || e), 'err');
    }
  });
}

/** Config modal: membership table + rename. */
export function _renderCfgProjectSets() {
  const el = document.getElementById('cfg-project-sets');
  if (!el) return;
  const projects = (S.status && S.status.projects) || S.projects || [];
  const map = (S.status && S.status.project_set_map) || S.projectSetMap || {};
  const flat = (S.projectSets && S.projectSets.flat) || {};
  const paths = Object.keys(flat).sort();
  let html = '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr>'
    + '<th style="padding:4px 6px;text-align:left">project</th>'
    + '<th style="padding:4px 6px;text-align:left">' + i18n.t('setfilter.setPath') + '</th>'
    + '</tr></thead><tbody>';
  for (const proj of projects) {
    const cur = map[proj] || '';
    const opts = ['<option value="">(unset)</option>']
      .concat(paths.map(p => `<option value="${escHtml(p)}"${p === cur ? ' selected' : ''}>${escHtml(p)}</option>`));
    if (cur && !paths.includes(cur)) {
      opts.push(`<option value="${escHtml(cur)}" selected>${escHtml(cur)}</option>`);
    }
    html += `<tr><td style="padding:4px 6px;font-weight:500">${escHtml(proj)}</td>`
      + `<td style="padding:4px 6px"><select data-proj-set="${escHtml(proj)}" style="min-width:160px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px">`
      + opts.join('')
      + `</select>`
      + ` <input type="text" placeholder="${i18n.t('setfilter.typePath')}" data-proj-set-custom="${escHtml(proj)}" style="width:120px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px">`
      + ` <button type="button" class="cfg-btn" data-click="${tqAct([['tqAssignProjectSet', proj]])}">${i18n.t('setfilter.apply')}</button>`
      + `</td></tr>`;
  }
  html += '</tbody></table>';
  html += '<div style="margin-top:10px;display:flex;gap:6px;align-items:center;flex-wrap:wrap">'
    + '<span class="filter-label">rename set:</span>'
    + '<input id="cfg-set-rename-from" type="text" placeholder="from" style="width:120px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px">'
    + '<input id="cfg-set-rename-to" type="text" placeholder="to" style="width:120px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px">'
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tq' + i18n.t('setfilter.rename') + 'ProjectSet']])}">${i18n.t('setfilter.rename')}</button>`
    + '</div>';
  el.innerHTML = html;
}

export async function tqAssignProjectSet(proj) {
  const sel = document.querySelector(`select[data-proj-set="${CSS.escape(proj)}"]`);
  const custom = document.querySelector(`input[data-proj-set-custom="${CSS.escape(proj)}"]`);
  let path = custom && custom.value.trim() ? custom.value.trim() : (sel ? sel.value : '');
  const body = { set: path ? path : null };
  try {
    const r = await fetch(`/api/project/${encodeURIComponent(proj)}/set`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.ok === false) {
      showToast(data.error || `HTTP ${r.status}`, 'err');
      return;
    }
    showToast(`${proj} → ${path || '(unset)'}`, 'ok');
    if (typeof refresh === 'function') await refresh();
    _renderCfgProjectSets();
    _populateSetBar();
  } catch (e) {
    showToast(String(e && e.message || e), 'err');
  }
}

export async function tqRenameProjectSet() {
  const fromEl = document.getElementById('cfg-set-rename-from');
  const toEl = document.getElementById('cfg-set-rename-to');
  const from = fromEl && fromEl.value.trim();
  const to = toEl && toEl.value.trim();
  if (!from || !to) {
    showToast(i18n.t('setfilter.fromToRequired'), 'err');
    return;
  }
  try {
    const r = await fetch('/api/project-sets/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from, to }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.ok === false) {
      showToast(data.error || `HTTP ${r.status}`, 'err');
      return;
    }
    showToast(i18n.t('setfilter.renamed', {from, to, n: (data.projects || []).length}), 'ok');
    if (typeof refresh === 'function') await refresh();
    _renderCfgProjectSets();
    _populateSetBar();
  } catch (e) {
    showToast(String(e && e.message || e), 'err');
  }
}
