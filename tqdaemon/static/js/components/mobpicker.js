// Mobile FAB task picker: lists runnable tasks and force-runs the chosen one.
// Uses app.js globals at call time (i18n, badge, refresh, _postJson, openAlert/showToast, ...).

import { latest } from '../core/api.js';

// W6.7: mobile FAB task picker
export function openMobTaskPicker() {
  const picker = document.getElementById('mob-task-picker');
  if (picker) picker.classList.add('open');
  const s = document.getElementById('mob-task-search');
  if (s) { s.value = ''; s.focus(); }
  renderMobTaskList().catch(() => {});
}
export function closeMobTaskPicker() {
  const picker = document.getElementById('mob-task-picker');
  if (picker) picker.classList.remove('open');
}
export async function renderMobTaskList() {
  const list = document.getElementById('mob-task-list');
  if (!list) return;
  const live = latest('mob-task-list');
  list.innerHTML = `<div style="color:var(--muted);text-align:center;padding:16px">…</div>`;
  let all = [];
  try {
    const statuses = ['queued','stopped','failed','unknown','interrupted','suspended','cancelled'];
    const results = await Promise.all(statuses.map(s =>
      fetch(`/api/queue?status=${s}&limit=100`).then(r => r.json()).catch(() => [])
    ));
    const seen = new Set();
    for (const r of results) {
      for (const t of (Array.isArray(r) ? r : (r.tasks || []))) {
        const k = t.project + '/' + t.task_id;
        if (!seen.has(k)) { seen.add(k); all.push(t); }
      }
    }
  } catch(e) { all = []; }
  if (!live()) return;
  const q = ((document.getElementById('mob-task-search') || {}).value || '').toLowerCase();
  const runnable = (!q ? all : all.filter(t =>
    (t.project + '/' + t.task_id + ' ' + (t.description||'')).toLowerCase().includes(q)
  )).slice(0, 30);
  if (!runnable.length) {
    list.innerHTML = `<div style="color:var(--muted);text-align:center;padding:16px">${i18n.t('mobile.noRunnableTasks')}</div>`;
    return;
  }
  list.innerHTML = runnable.map(t => `
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:10px 12px;display:flex;align-items:center;gap:8px">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          ${_taskCopyIconBtn(t.project, t.task_id)}<span style="color:var(--muted)">${escHtml(t.project)}/</span>${escHtml(t.task_id)}
        </div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">${badge(t.status)}</div>
        ${t.description ? `<div style="font-size:11px;color:var(--text2);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(t.description)}</div>` : ''}
      </div>
      <button class="action-btn blue" data-click="${tqAct([['mobForceTask', String(t.project), String(t.task_id)]])}">${i18n.t('btn.run')}</button>
    </div>`).join('');
}
export async function mobForceTask(project, taskId) {
  const btn = (typeof event !== 'undefined' && event && event.target && event.target.closest)
    ? event.target.closest('button') : null;
  if (btn && !_btnPending(btn, i18n.t('btn.forcing'))) return;
  closeMobTaskPicker();
  try {
    await _postJson(`/api/force/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}`);
    _optimisticQueueStatus(project, taskId, 'queued');
    showToast(i18n.t('toast.forced'), 'ok');
  } catch (e) {
    showToast(String(e.message || e), 'err');
    openAlert(String(e.message || e), i18n.t('modal.forceRunTitle'));
  } finally {
    if (btn) _btnDone(btn);
  }
  refresh();
}
