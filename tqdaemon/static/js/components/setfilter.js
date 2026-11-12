// @ts-check
// Project-set tree filter (multilevel slash paths) — distinct from task YAML group:.
// Uses app.js globals at call time (tqAct, i18n, refresh, openConfirm, showToast, _syncUrl, ...).
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';
import { _postJson } from './cfgmodal.js';

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
    + `<button class="filter-chip${active ? ' active' : ''}" data-set="${escHtml(path)}" data-click="${tqAct([['setProjectSet', path]])}"${_capTitle(path)}>${escHtml(node.name || path)} (${node.subtree_count || 0})${_capBadge(path)}</button>`
    + `</span>`;
  if (expanded) {
    for (const c of kids) html += _treeNodeHtml(c, depth + 1);
  }
  return html;
}

/** "running/max" badge on a set chip when the set has a subtree max_tasks total. */
function _capBadge(path) {
  const node = ((S.projectSets && S.projectSets.flat) || {})[path];
  const cap = node && node.limits ? Number(node.limits.max_tasks || 0) : 0;
  if (!cap) return '';
  const run = Number(node.running || 0);
  const cls = run >= cap ? 'set-cap set-cap-full' : 'set-cap';
  return ` <span class="${cls}">${run}/${cap}</span>`;
}

function _capTitle(path) {
  const node = ((S.projectSets && S.projectSets.flat) || {})[path];
  if (!node) return '';
  const lines = [];
  const parts = Object.entries(node.limits || {}).filter(([, v]) => Number(v) > 0).map(([k, v]) => `${k}=${v}`);
  if (parts.length) lines.push(i18n.t('setfilter.subtreeTotals') + ': ' + parts.join(', '));
  const why = _whyWaiting(node);
  if (why) lines.push(why);
  const gpu = _gpuBudget(node);
  if (gpu) lines.push(gpu);
  return lines.length ? ` title="${escHtml(lines.join('\n'))}"` : '';
}

/** "12 waiting: resources 7, deps 3, …" for a set node (codes -> friendly labels). */
export function _whyWaiting(node) {
  if (!node || !node.queued) return '';
  const label = c => (c === 'queued' ? i18n.t('setfilter.readyToStart') : i18n.t('errlabel.' + c));
  const parts = (node.waiting || []).map(w => `${label(w.code)} ${w.n}`);
  return i18n.t('setfilter.whyWaiting', { n: node.queued, reasons: parts.join(', ') });
}

/** "GPU today 3.2 / 8 h" when the set has a daily GPU budget (or any GPU use today). */
export function _gpuBudget(node) {
  if (!node) return '';
  const cap = node.limits ? Number(node.limits.max_gpu_hours_day || 0) : 0;
  const used = Number(node.gpu_hours_today || 0);
  if (!cap && !used) return '';
  return cap ? i18n.t('setfilter.gpuBudget', { used: used.toFixed(1), cap }) : i18n.t('setfilter.gpuUsed', { used: used.toFixed(1) });
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
    + `<button class="filter-chip${allActive ? ' active' : ''}" data-set="" data-click="${tqAct([['setProjectSet', '']])}">${i18n.t('filter.all')}</button>`;
  for (const r of roots) html += _treeNodeHtml(r, 0);
  if (unset.subtree_count > 0) {
    html += `<button class="filter-chip${unsetActive ? ' active' : ''}" data-set="${UNSET}" data-click="${tqAct([['setProjectSet', UNSET]])}">${i18n.t('setfilter.unsetCount', {n: unset.subtree_count})}</button>`;
  }
  bar.innerHTML = html;
  bar.style.display = '';
  _updateSetBulkBar();
  // Header picker lists the same tree.
  if (S.projects && S.projects.length && typeof _populateProjectSelect === 'function') _populateProjectSelect(S.projects);
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
  const label = multi ? i18n.t('setfilter.bulkProjects', { n }) : i18n.t('setfilter.bulkSet', { set: S.setFilter, n });
  bar.hidden = false;
  bar.innerHTML = `<span class="filter-label">${escHtml(label)}:</span>`
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSetBulk', 'freeze']])}">${i18n.t('setfilter.freeze')}</button>`
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSetBulk', 'unfreeze']])}">${i18n.t('setfilter.unfreeze')}</button>`
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSetBulk', 'pause']])}">${i18n.t('setfilter.pause')}</button>`
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSetBulk', 'resume']])}">${i18n.t('setfilter.resume')}</button>`
    + `<button type="button" class="cfg-btn" style="color:var(--fail)" data-click="${tqAct([['tqSetBulk', 'stop-all']])}">${i18n.t('setfilter.stopAll')}</button>`;
}

const _BULK_LABEL = { 'stop-all': 'setfilter.stopAll' };

export function tqSetBulk(action) {
  const multi = Array.isArray(S.selectedProjects) && S.selectedProjects.length > 0;
  const targets = _bulkTargetProjects();
  if (!targets.length) return;
  const n = targets.length;
  const label = i18n.t(_BULK_LABEL[action] || 'setfilter.' + action);
  const scope = multi ? i18n.t('setfilter.scopeSelected', { n }) : i18n.t('setfilter.scopeSet', { n, set: S.setFilter });
  openConfirm(i18n.t('setfilter.confirmBulk', { action: label, scope }), async () => {
    try {
      const data = multi
        ? await _postJson('/api/projects/bulk', { projects: targets, action })
        : await _postJson(`/api/project-set/${action}`, { set: S.setFilter });
      showToast(i18n.t('setfilter.bulkDone', { action: label, n: (data.projects || targets).length }), 'ok');
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
    + '<th style="padding:4px 6px;text-align:left">' + i18n.t('setfilter.project') + '</th>'
    + '<th style="padding:4px 6px;text-align:left">' + i18n.t('setfilter.setPath') + '</th>'
    + '</tr></thead><tbody>';
  for (const proj of projects) {
    const cur = map[proj] || '';
    const opts = [`<option value="">${escHtml(i18n.t('setfilter.unset'))}</option>`]
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
    + `<span class="filter-label">${escHtml(i18n.t('setfilter.renameSet'))}</span>`
    + `<input id="cfg-set-rename-from" type="text" class="cfg-input" style="width:120px" placeholder="${escHtml(i18n.t('setfilter.from'))}">`
    + `<input id="cfg-set-rename-to" type="text" class="cfg-input" style="width:120px" placeholder="${escHtml(i18n.t('setfilter.to'))}">`
    + `<button type="button" class="cfg-btn" data-click="${tqAct([['tqRenameProjectSet']])}">${i18n.t('setfilter.rename')}</button>`
    + '</div>';
  html += _cfgNodesHtml(flat, paths);
  el.innerHTML = html;
}

/** Tree nodes: create empty nodes (limits can live on them), remove empty ones, see usage. */
function _cfgNodesHtml(flat, paths) {
  let html = `<div class="set-nodes"><div class="filter-label">${escHtml(i18n.t('setfilter.nodes'))}</div><ul class="set-node-list">`;
  for (const p of paths) {
    const node = flat[p] || {};
    const depth = p.split('/').length - 1;
    const cap = node.limits ? Number(node.limits.max_tasks || 0) : 0;
    const usage = (cap ? ` · ${Number(node.running || 0)}/${cap} ${escHtml(i18n.t('setfilter.running'))}` : '')
      + (node.queued ? ` · ${escHtml(_whyWaiting(node))}` : '')
      + (_gpuBudget(node) ? ` · ${escHtml(_gpuBudget(node))}` : '');
    const rm = (node.subtree_count || 0) === 0
      ? ` <button type="button" class="cfg-btn" data-click="${tqAct([['tqRemoveSetNode', p]])}">${escHtml(i18n.t('setfilter.removeNode'))}</button>`
      : '';
    html += `<li style="--depth:${depth}"><b>${escHtml(p.split('/').pop())}</b> <span class="set-node-path">${escHtml(p)}</span>`
      + ` (${node.subtree_count || 0})${usage}${rm}</li>`;
  }
  html += '</ul><div class="set-node-add">'
    + `<input id="cfg-set-node-new" type="text" class="cfg-input" placeholder="${escHtml(i18n.t('setfilter.newNodePlaceholder'))}">`
    + ` <button type="button" class="cfg-btn" data-click="${tqAct([['tqAddSetNode']])}">${escHtml(i18n.t('setfilter.addNode'))}</button>`
    + `<div class="set-node-hint">${escHtml(i18n.t('setfilter.totalsHint'))}</div></div></div>`;
  return html;
}

async function _postSetNode(action, path) {
  try {
    await _postJson('/api/project-sets/node', { action, path });
    if (typeof refresh === 'function') await refresh();
    _renderCfgProjectSets();
    _populateSetBar();
    return true;
  } catch (e) {
    showToast(String(e && e.message || e), 'err');
    return false;
  }
}

export async function tqAddSetNode() {
  const el = /** @type {HTMLInputElement | null} */ (document.getElementById('cfg-set-node-new'));
  const path = el && el.value.trim();
  if (!path) return;
  if (await _postSetNode('add', path)) showToast(i18n.t('setfilter.nodeAdded', { path }), 'ok');
}

export async function tqRemoveSetNode(path) {
  if (await _postSetNode('remove', path)) showToast(i18n.t('setfilter.nodeRemoved', { path }), 'ok');
}

export async function tqAssignProjectSet(proj) {
  const sel = /** @type {HTMLInputElement | null} */ (document.querySelector(`select[data-proj-set="${CSS.escape(proj)}"]`));
  const custom = /** @type {HTMLInputElement | null} */ (document.querySelector(`input[data-proj-set-custom="${CSS.escape(proj)}"]`));
  let path = custom && custom.value.trim() ? custom.value.trim() : (sel ? sel.value : '');
  try {
    await _postJson(`/api/project/${encodeURIComponent(proj)}/set`, { set: path ? path : null });
    showToast(`${proj} → ${path || i18n.t('setfilter.unset')}`, 'ok');
    if (typeof refresh === 'function') await refresh();
    _renderCfgProjectSets();
    _populateSetBar();
  } catch (e) {
    showToast(String(e && e.message || e), 'err');
  }
}

export async function tqRenameProjectSet() {
  const fromEl = /** @type {HTMLInputElement | null} */ (document.getElementById('cfg-set-rename-from'));
  const toEl = /** @type {HTMLInputElement | null} */ (document.getElementById('cfg-set-rename-to'));
  const from = fromEl && fromEl.value.trim();
  const to = toEl && toEl.value.trim();
  if (!from || !to) {
    showToast(i18n.t('setfilter.fromToRequired'), 'err');
    return;
  }
  try {
    const data = await _postJson('/api/project-sets/rename', { from, to });
    showToast(i18n.t('setfilter.renamed', {from, to, n: (data.projects || []).length}), 'ok');
    if (typeof refresh === 'function') await refresh();
    _renderCfgProjectSets();
    _populateSetBar();
  } catch (e) {
    showToast(String(e && e.message || e), 'err');
  }
}
