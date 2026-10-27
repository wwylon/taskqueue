// Schedule tab. Uses app.js globals at call time: tqAct, TQ_THIS, i18n, badge, showToast, openYamlModal,
// tqProjectQuery, _postJson, _showPageEmpty.
import { S } from '../core/state.js';
import { escHtml, fmt } from '../util/format.js';
import { fetchJson } from '../core/api.js';
import { projStyle } from '../util/projcolor.js';


export async function loadSchedules() {
  const data = await fetchJson('/api/schedules' + tqProjectQuery(), 8000, true);
  if (!data) return false;
  const rows = data.schedules || [];
  const tb = document.getElementById('sched-tbody');
  if (!tb) return;
  _showPageEmpty('sched-empty', 'sched-wrap', rows.length === 0);
  tb.innerHTML = rows.map(r => {
    // JSON key: a NUL separator does not survive an HTML attribute (parsers turn it into U+FFFD).
    const key = JSON.stringify([String(r.project), String(r.task_id)]);
    const sel = key === S.schedKey ? ' class="qrow-kbd"' : '';
    const pj = `<span class="proj-tag" style="${projStyle(r.project)}">${escHtml(r.project)}</span>`;
    const tid = escHtml(r.task_id);
    return `<tr${sel} data-sched="${escHtml(key)}" data-click="${tqAct([['tqSelectSchedule', TQ_THIS]])}">
      <td>${pj}</td>
      <td class="task-id-mono">${tid}</td>
      <td class="task-id-mono">${escHtml(r.cron)}</td>
      <td>${badge(r.status)}${r.enabled ? '' : ' <span class="badge badge-disabled">' + escHtml(i18n.t('sched.pausedBadge')) + '</span>'}</td>
      <td>${escHtml(r.next_fire ? fmt(r.next_fire) : '—')}</td>
      <td>
        <button type="button" class="cfg-btn" data-click="${tqAct([['tqSchedRun', String(r.project), String(r.task_id)]])}" data-click-stop>${escHtml(i18n.t('sched.run'))}</button>
        <button type="button" class="cfg-btn" data-click="${tqAct([['openYamlModal', String(r.project), String(r.task_id)]])}" data-click-stop>${escHtml(i18n.t('sched.edit'))}</button>
        ${r.enabled
          ? `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSchedPause', String(r.project), String(r.task_id)]])}" data-click-stop>${escHtml(i18n.t('sched.pause'))}</button>`
          : `<button type="button" class="cfg-btn" data-click="${tqAct([['tqSchedResume', String(r.project), String(r.task_id)]])}" data-click-stop>${escHtml(i18n.t('sched.resume'))}</button>`}
      </td>
    </tr>`;
  }).join('');
}

export function tqSelectSchedule(row) {
  S.schedKey = row.getAttribute('data-sched') || '';
  document.querySelectorAll('#sched-tbody tr').forEach(tr => tr.classList.toggle('qrow-kbd', tr === row));
}

export function tqNewSchedule() {
  const note = document.getElementById('sched-note');
  if (note) note.hidden = false;
  if (S.schedKey) {
    let parts = [];
    try { parts = JSON.parse(S.schedKey); } catch (_) {}
    if (parts[0] && parts[1]) openYamlModal(parts[0], parts[1]);
  }
}

export function tqSchedRun(project, taskId) {
  return toastResult(_postJson('/api/retry/' + encodeURIComponent(project) + '/' + encodeURIComponent(taskId)),
    i18n.t('toast.retried'), loadSchedules);
}

export function tqSchedResume(project, taskId) {
  return toastResult(_postJson('/api/enable/' + encodeURIComponent(project) + '/' + encodeURIComponent(taskId) + '?enabled=1'),
    i18n.t('sched.resumed'), loadSchedules);
}

export function tqSchedPause(project, taskId) {
  return toastResult(_postJson('/api/enable/' + encodeURIComponent(project) + '/' + encodeURIComponent(taskId) + '?enabled=0'),
    i18n.t('sched.paused'), loadSchedules);
}
