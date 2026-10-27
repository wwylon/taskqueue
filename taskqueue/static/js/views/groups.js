// Groups tab and the queue's group filter chips. Uses app.js globals at call time: tqAct, TQ_THIS, i18n, _btnPending,
// _btnDone, openConfirm, openAlert, refresh, setProject, tab, _syncUrl, _resetQueueSearchForFilter.
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';
import { projStyle } from '../util/projcolor.js';

export async function loadGroups() {
  const proj = S.selectedProject && S.selectedProject !== 'all' ? S.selectedProject : '';
  const qs = proj ? `?project=${encodeURIComponent(proj)}` : '';
  const el = document.getElementById('groups-content');
  if (!el) return;
  try {
    const data = await fetch('/api/groups' + qs).then(r => r.ok ? r.json() : Promise.resolve({}));
    const groups = (data.groups || []).sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    if (!groups.length) { el.innerHTML = `<p style="color:var(--muted)">${i18n.t('groups.noGroups')}</p>`; return; }
    const STATUS_ORDER = ['running','queued','failed','unknown','interrupted','suspended','stopped','cancelled','completed','disabled'];
    el.innerHTML = groups.map(g => {
      const apiName = g.name;  // real DB value, may be ""
      const apiProject = g.project || '';  // set for ungrouped rows: bulk ops are scoped to one project
      // Project to select in the queue: the row's own, else the only project using the group, else keep the current one.
      const gProjects = g.projects || [];
      const viewProject = apiProject || (gProjects.length === 1 ? gProjects[0] : (gProjects.includes(S.selectedProject) ? S.selectedProject : ''));
      const groupLabel = escHtml(g.name || '(ungrouped)');
      const projTag = g.project
        ? ` <span class="proj-tag" style="display:inline;${projStyle(g.project)}">${escHtml(g.project)}</span>`
        : (gProjects.length
          ? ' ' + gProjects.map(p => `<span class="proj-tag" style="display:inline;${projStyle(p)}">${escHtml(p)}</span>`).join(' ')
          : '');
      const byStatus = g.by_status || {};
      const badges = STATUS_ORDER.filter(s => byStatus[s]).map(s =>
        `<span style="display:inline-block;margin:2px;padding:2px 8px;border-radius:10px;font-size:11px;background:var(--bg3);color:var(--text)">${s}: ${byStatus[s]}</span>`
      ).join('');
      const btnStyle = 'font-size:11px;padding:3px 10px;border:1px solid rgba(255,255,255,.2);border-radius:4px;cursor:pointer;margin-left:6px;font-weight:600;';
      return `<div style="padding:12px 0;border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <strong style="font-size:14px">${groupLabel}${projTag}</strong>
          <span style="color:var(--muted);font-size:12px">${i18n.t('groups.tasksCount', {n: g.total})}</span>
          ${badges}
          <button style="${btnStyle}background:var(--bg3);color:var(--text)" data-click="${tqAct([['viewGroupInQueue', apiName, viewProject]])}">${i18n.t('groups.btnViewQueue')}</button>
          <button style="${btnStyle}background:#c0392b;color:#fff" data-click="${tqAct([['groupStop', TQ_THIS, apiName, apiProject]])}">${i18n.t('groups.btnStopAll')}</button>
          <button style="${btnStyle}background:#2980b9;color:#fff" data-click="${tqAct([['groupRetry', TQ_THIS, apiName, false, apiProject]])}">${i18n.t('groups.btnRetryFailed')}</button>
          <button style="${btnStyle}background:#6d28d9;color:#fff" data-click="${tqAct([['groupRetry', TQ_THIS, apiName, true, apiProject]])}">${i18n.t('groups.btnRestartAll') || 'Restart all'}</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<p style="color:red">Error: ${escHtml(String(e))}</p>`; }
}

export function groupStop(btn, name, project) {
  if (!_btnPending(btn, i18n.t('btn.stopping'))) return;
  let settled = false;
  const release = () => {
    if (settled) return;
    settled = true;
    _btnDone(btn);
  };
  openConfirm(`Stop all running tasks in group "${name || '(ungrouped)'}"${project ? ` of project ${project}` : ''}?`, async () => {
    try {
      const pq = project ? `?project=${encodeURIComponent(project)}` : '';
      const r = await fetch(`/api/groups/${encodeURIComponent(name)}/stop${pq}`, {method:'POST'});
      const j = await r.json().catch(() => ({}));
      openAlert(i18n.t('alert.stoppedTasks', {n: j.stopped||0}));
      loadGroups();
    } finally {
      release();
    }
  }, {
    title: i18n.t('modal.stopGroupTitle'),
    confirmLabel: i18n.t('groups.btnStopAll'),
    onCancel: release,
  });
}

export async function groupRetry(btn, name, includeCompleted, project) {
  if (!_btnPending(btn, '…')) return;
  try {
    const base = `/api/groups/${encodeURIComponent(name)}/retry`;
    const q = new URLSearchParams();
    if (project) q.set('project', project);
    if (includeCompleted) {
      // Restart all re-runs COMPLETED tasks: show the exact count first and require confirmation
      // (2026-10-02: an unconfirmed, unscoped restart-all re-ran ~70 finished tasks).
      q.set('completed', '1');
      q.set('dry_run', '1');
      const pre = await fetch(`${base}?${q}`, {method:'POST'}).then(r => r.json().catch(() => ({})));
      q.delete('dry_run');
      if (!pre.ok) { openAlert(pre.error || 'Restart all refused'); return; }
      if (!pre.would_retry) { openAlert(i18n.t('alert.retriedTasks', {n: 0})); return; }
      const label = `${name || '(ungrouped)'}${project ? ` (project ${project})` : ''}`;
      const go = await new Promise(resolve => openConfirm(
        `Restart ${pre.would_retry} task(s) in "${label}", including ${pre.completed_included} COMPLETED task(s)? ` +
        `They will run again and may overwrite their outputs.`,
        () => resolve(true),
        {title: 'Restart all', confirmLabel: i18n.t('groups.btnRestartAll') || 'Restart all', onCancel: () => resolve(false)}
      ));
      if (!go) return;
      q.set('confirm', String(pre.would_retry));
    }
    const qs = q.toString() ? `?${q}` : '';
    const r = await fetch(`${base}${qs}`, {method:'POST'});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { openAlert(j.error || `Retry refused (${r.status})`); return; }
    openAlert(i18n.t('alert.retriedTasks', {n: j.retried||0}));
    loadGroups(); refresh();
  } finally {
    _btnDone(btn);
  }
}

// ---- Group filter chips (below status + project) ----
let _groupChipRows = [];

export async function _populateGroupChips() {
  const bar = document.getElementById('queue-group-bar');
  if (!bar) return;
  const proj = S.selectedProject !== 'all' ? S.selectedProject : '';
  const qs = proj ? `?project=${encodeURIComponent(proj)}` : '';
  try {
    const data = await fetch('/api/groups' + qs).then(r => r.ok ? r.json() : {});
    _groupChipRows = data.groups || [];
  } catch (e) {
    _groupChipRows = [];
  }
  _updateGroupChips();
}

export function _updateGroupChips() {
  const bar = document.getElementById('queue-group-bar');
  if (!bar) return;
  const named = _groupChipRows.filter(g => g.name);
  const hasUngrouped = _groupChipRows.some(g => !g.name && g.total > 0);
  if (!named.length && !hasUngrouped) {
    bar.style.display = 'none';
    bar.innerHTML = '';
    return;
  }
  const expanded = bar.dataset.expanded === '1';
  const cur = S.groupFilter;
  const label = String(i18n.t('filter.labelGroup') || 'group').replace(/[:：]\s*$/, '');
  let html = `<span class="filter-label" data-click="${tqAct([['_toggleGroupBar']])}" style="cursor:pointer;user-select:none">${escHtml(label)}${expanded ? ' ▴' : ' ▾'}:</span>`;
  if (expanded) {
    html += `<button class="filter-chip${!cur ? ' active' : ''}" data-group="" data-click="${tqAct([['setGroupFilter', null, TQ_THIS]])}">${i18n.t('filter.all')}</button>`;
    named.sort((a, b) => (a.name || '').localeCompare(b.name || '')).forEach(g => {
      const active = cur === g.name;
      html += `<button class="filter-chip${active ? ' active' : ''}" data-group="${escHtml(g.name)}" data-click="${tqAct([['setGroupFilter', String(g.name), TQ_THIS]])}">${escHtml(g.name)} <span style="opacity:.7">(${g.total})</span></button>`;
    });
    if (hasUngrouped) {
      const active = cur === '__ungrouped__';
      html += `<button class="filter-chip${active ? ' active' : ''}" data-group="__ungrouped__" data-click="${tqAct([['setGroupFilter', '__ungrouped__', TQ_THIS]])}">${i18n.t('filter.ungrouped')}</button>`;
    }
  }
  bar.innerHTML = html;
  bar.style.display = '';
}

export function _toggleGroupBar() {
  const bar = document.getElementById('queue-group-bar');
  if (!bar) return;
  bar.dataset.expanded = bar.dataset.expanded === '1' ? '0' : '1';
  _updateGroupChips();
}

export function setGroupFilter(name, btn) {
  S.groupFilter = name || null;
  S.queuePage = 0;
  _resetQueueSearchForFilter();
  _updateGroupChips();
  refresh();
  if (!S.applyingUrl) _syncUrl({ replace: false });
}
export function viewGroupInQueue(name, project) {
  const g = (!name || name === '(ungrouped)') ? '__ungrouped__' : name;
  S.groupFilter = g;
  S.queuePage = 0;
  // Select the group's project too; the group is set first so the refresh this triggers already uses it.
  if (project && project !== S.selectedProject) setProject(project, { keepGroup: true });
  const groupBar = document.getElementById('queue-group-bar');
  if (groupBar) groupBar.dataset.expanded = '1';
  const navBtn = document.querySelector('nav button[data-tab="queue"]') || document.querySelector('nav button');
  tab('queue', navBtn);
  _populateGroupChips().then(() => refresh());
}
