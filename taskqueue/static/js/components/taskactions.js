// Task and project actions: stop/retry/force/mark-processed/enable, priority editing, project freeze/stop-all,
// worker sync and the project root bar. Uses app.js globals at call time (refresh, renderQueue, _postJson,
// openConfirm/openAlert/showToast, _dagRefreshAfterAction, ...).
import { S } from '../core/state.js';
import { fetchJson } from '../core/api.js';
import { qrowId } from '../util/format.js';

// ---- Task actions ----

// F2: retry with new endpoint (for failed/interrupted/stopped only)
export async function retryTaskNew(project, taskId, btn) {
  if (!_btnPending(btn, '…')) return;
  const qrow = document.getElementById(qrowId(project, taskId));
  const badgeEl = qrow && qrow.querySelector('.badge');
  const prevClass = badgeEl ? badgeEl.className : '';
  const prevText = badgeEl ? badgeEl.textContent : '';
  try {
    const r = await fetch(`/api/tasks/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}/retry`, {method:'POST'});
    const data = await r.json();
    if (r.ok && data.ok) {
      // Optimistic: flip badge
      if (badgeEl) { badgeEl.className = 'badge badge-queued'; badgeEl.textContent = 'queued'; }
      setTimeout(refresh, 500);
    } else {
      // F-130: restore optimistic badge on failure
      if (badgeEl) { badgeEl.className = prevClass; badgeEl.textContent = prevText; }
      openAlert(data.error || i18n.t('alert.retryFailed'), i18n.t('modal.retryFailedTitle'));
    }
  } catch (e) {
    if (badgeEl) { badgeEl.className = prevClass; badgeEl.textContent = prevText; }
    openAlert(String(e), i18n.t('modal.retryFailedTitle'));
  } finally {
    _btnDone(btn);
  }
}

export async function copyTaskYaml(project, taskId, btn) {
  try {
    const r = await fetch(`/api/tasks/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}/yaml`);
    if (!r.ok) { openAlert('YAML not found'); return; }
    const text = await r.text();
    await navigator.clipboard.writeText(text);
    const orig = btn.textContent;
    btn.textContent = i18n.t('btn.copied');
    setTimeout(() => { btn.textContent = orig; }, 1500);
  } catch(e) {
    openAlert('Copy failed: ' + e, i18n.t('modal.copyFailedTitle'));
  }
}

// N2: inline priority editor
export function startPrioEdit(span, project, taskId, currentPrio) {
  const inp = document.createElement('input');
  inp.type = 'number'; inp.min = 0; inp.max = 100;
  inp.value = currentPrio;
  inp.style.cssText = 'width:50px;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:2px 6px;border-radius:5px;font-size:12px;text-align:center';
  span.replaceWith(inp);
  inp.focus(); inp.select();
  async function commit() {
    const newPrio = parseInt(inp.value);
    if (isNaN(newPrio) || newPrio < 0 || newPrio > 100) { revert(); return; }
    try {
      const r = await fetch(`/api/tasks/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}/priority`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({priority: newPrio}),
      });
      if (r.ok) {
        // Update in S.queue
        const t = S.queue.find(x => x.project === project && x.task_id === taskId);
        if (t) t.priority = newPrio;
        renderQueue();
        return;
      }
    } catch(e) {}
    revert();
  }
  function revert() {
    const newSpan = document.createElement('span');
    newSpan.className = 'prio-span';
    newSpan.style.cssText = 'cursor:pointer;padding:2px 6px;border-radius:5px;background:var(--bg3);border:1px solid var(--border);font-size:12px';
    newSpan.title = i18n.t('task.editPriority');
    newSpan.textContent = currentPrio;
    newSpan.onclick = () => startPrioEdit(newSpan, project, taskId, currentPrio);
    inp.replaceWith(newSpan);
  }
  inp.addEventListener('blur', commit);
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') { inp.blur(); } else if (e.key === 'Escape') { inp.removeEventListener('blur', commit); revert(); } });
}

export function _btnPending(btn, label) {
  if (!btn) return false;
  if (btn.disabled) return false;
  btn.disabled = true;
  btn._origText = btn.textContent;
  btn.textContent = label || '…';
  // Safety timeout: re-enable after 8s in case of error / no refresh
  clearTimeout(btn._pendingTimer);
  btn._pendingTimer = setTimeout(() => {
    btn.disabled = false;
    btn.textContent = btn._origText;
  }, 8000);
  return true;
}
export function _btnDone(btn) {
  if (!btn) return;
  clearTimeout(btn._pendingTimer);
  btn.disabled = false;
  if (btn._origText != null) btn.textContent = btn._origText;
}
export function _optimisticQueueStatus(project, taskId, status) {
  const t = (S.queue || []).find(x => x.project === project && x.task_id === taskId);
  if (t) t.status = status;
  renderQueue();
}
/** History: re-queue a run's task with the args that run used (POST /api/runs/<id>/rerun). */
export function tqRerunRun(runId) {
  openConfirm(i18n.t('history.rerunConfirm').replace('{id}', runId), () =>
    toastResult(_postJson(`/api/runs/${encodeURIComponent(runId)}/rerun`), i18n.t('toast.retried'), refresh));
}

// Stop is sent after a short grace period with an Undo toast (a stopped run cannot be resumed
// mid-step). window.tqStopUndoMs overrides the delay; 0 sends at once.
function _stopUndoMs() {
  const v = typeof window !== 'undefined' ? window.tqStopUndoMs : undefined;
  return v == null ? 5000 : Number(v) || 0;
}

export async function stopTask(btn, project, taskId) {
  if (!_btnPending(btn, i18n.t('btn.stopping'))) return;
  const delay = _stopUndoMs();
  if (delay > 0) {
    const undone = await new Promise(resolve => {
      let settled = false;
      showUndoToast(i18n.t('toast.stopPending').replace('{task}', project + '/' + taskId),
        () => { if (!settled) { settled = true; resolve(true); } }, delay);
      setTimeout(() => { if (!settled) { settled = true; resolve(false); } }, delay);
    });
    if (undone) {
      _btnDone(btn);
      showToast(i18n.t('toast.stopUndone'), 'info');
      return;
    }
  }
  try {
    await _postJson(`/api/stop/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}`);
    showToast(i18n.t('toast.stopped'), 'ok');
  } catch (e) {
    showToast(String(e.message || e), 'err');
  } finally { _btnDone(btn); }
  refresh();
}
export async function retryTask(project, taskId, btn) {
  if (!_btnPending(btn, i18n.t('btn.retrying'))) return;
  try {
    await _postJson(`/api/retry/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}`);
    _optimisticQueueStatus(project, taskId, 'queued');
    showToast(i18n.t('toast.retried'), 'ok');
    _btnDone(btn);
    refresh();
  } catch(e) {
    showToast(String(e.message || e), 'err');
    _btnDone(btn);
  }
}
const FORCE_WAIT_MS = 60000;
const FORCE_POLL_MS = 1000;

/** After a confirmed Force: show "Starting…" in place of the button, poll until the task runs (or give up). */
function _watchForce(project, taskId) {
  const key = project + '/' + taskId;
  S.forceStarting[key] = Date.now();
  const repaint = () => { try { renderQueue(); } catch (e) {} try { globalThis.tqPaintDrawerHead && globalThis.tqPaintDrawerHead(); } catch (e) {} };
  const stop = (toastKey) => {
    delete S.forceStarting[key];
    repaint();
    if (toastKey) showToast(i18n.t(toastKey), 'info');
  };
  repaint();
  const tick = async () => {
    if (!S.forceStarting[key]) return;
    try { await refresh(); } catch (e) {}
    // F-131: do not rely on scoped S.status.running (project filter can hide the task).
    let live = ((S.status && S.status.running) || []).some(r => r.project === project && r.task_id === taskId);
    let row = (S.queue || []).find(x => x.project === project && x.task_id === taskId);
    if (!live) {
      try {
        const detail = await fetchJson(`/api/tasks/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}`);
        const st = detail && detail.task && detail.task.status;
        if (st === 'running') live = true;
        else if (st && st !== 'queued') row = { status: st };
      } catch (e) {}
    }
    if (live || (row && row.status !== 'queued')) { stop(live ? 'toast.forceStarted' : null); return; }
    if (Date.now() - S.forceStarting[key] > FORCE_WAIT_MS) { stop('toast.forceTimeout'); return; }
    setTimeout(tick, FORCE_POLL_MS);
  };
  setTimeout(tick, FORCE_POLL_MS);
}

export function forceTask(btn, project, taskId) {
  // Disable immediately so repeated clicks can't open another confirm / re-fire.
  if (!_btnPending(btn, i18n.t('btn.forcing'))) return;
  let settled = false;
  const release = () => {
    if (settled) return;
    settled = true;
    _btnDone(btn);
  };
  openConfirm(i18n.t('confirm.forceRun', {project, taskId}), async () => {
    try {
      await _postJson(`/api/force/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}`);
      _optimisticQueueStatus(project, taskId, 'queued');
      showToast(i18n.t('toast.forceWaiting'), 'info');
      release();
      _watchForce(project, taskId);
    } catch (e) {
      showToast(String(e.message || e), 'err');
      openAlert(String(e.message || e), i18n.t('modal.forceRunTitle'));
      release();
    }
  }, {
    title: i18n.t('modal.forceRunTitle'),
    confirmLabel: i18n.t('btn.force'),
    onCancel: release,
  });
}
export async function markProcessed(btn, project, taskId) {
  if (!_btnPending(btn, '…')) return;
  try {
    await _postJson(`/api/mark-processed/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}`);
  } catch (e) {
    showToast(String(e.message || e), 'err');
  } finally { _btnDone(btn); }
  setTimeout(refresh, 500);
}
export async function enableTask(project, taskId, btn) {
  if (!_btnPending(btn, '…')) return;
  try {
    const r = await fetch(`/api/enable/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}`, {method:'POST'});
    const data = await r.json().catch(() => ({}));
    if (r.ok && data.ok) {
      setTimeout(refresh, 500);
    } else {
      openAlert(data.error || i18n.t('alert.enableFailed'), i18n.t('modal.enableFailedTitle'));
    }
  } finally { _btnDone(btn); }
}
export async function setPriority(project, taskId, prio) {
  await _postJson(`/api/priority/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}/${parseInt(prio)}`);
}

// W9.5: project freeze / stop-all
export function _updateFreezeBtn() {
  const pbtn = document.getElementById('proj-pause-btn');
  if (pbtn && S.selectedProject !== 'all') {
    const paused = !!(S.cfg && S.cfg[`project_paused_${S.selectedProject}`]);
    pbtn.textContent = paused ? i18n.t('btn.resumeProject') : i18n.t('btn.pauseProject');
    pbtn.style.color = paused ? 'var(--ok)' : '';
  }
  const btn = document.getElementById('proj-freeze-btn');
  if (!btn || S.selectedProject === 'all') return;
  // Frozen state stored in config as project_frozen_<name>
  const frozen = !!(S.cfg && S.cfg[`project_frozen_${S.selectedProject}`]);
  btn.textContent = frozen ? i18n.t('btn.unfreeze') : i18n.t('btn.freeze');
  btn.style.color = frozen ? 'var(--ok)' : '';
}

// W9.9: project root discovery
export async function _showProjectRoot(projName) {
  const bar = document.getElementById('proj-root-bar');
  const txt = document.getElementById('proj-root-text');
  if (!bar || !txt) return;
  if (projName === 'all') { bar.style.display = 'none'; return; }
  if (!Object.keys(S.projectRoots).length) {
    try {
      const r = await fetch('/api/project-roots');
      S.projectRoots = await r.json();
    } catch(e) { S.projectRoots = {}; }
  }
  const root = S.projectRoots[projName];
  const frozen = !!(S.cfg && S.cfg[`project_frozen_${projName}`]);
  const freezeNote = frozen ? ` · ${i18n.t('freeze.blockedBanner')}` : '';
  if (root) {
    txt.textContent = `📁 ${projName} root: ${root}${freezeNote}`;
    bar.style.display = '';
    bar.style.color = frozen ? 'var(--warn)' : '';
  } else {
    txt.textContent = `⚠ "${projName}" has no registered root path${freezeNote}`;
    bar.style.display = '';
    bar.style.color = frozen ? 'var(--warn)' : '';
  }
}

export async function toggleProjectFreeze() {
  if (S.selectedProject === 'all') return;
  const btn = document.getElementById('proj-freeze-btn');
  const frozen = !!(S.cfg && S.cfg[`project_frozen_${S.selectedProject}`]);
  const action = frozen ? 'unfreeze' : 'freeze';
  if (!_btnPending(btn, action === 'freeze' ? i18n.t('btn.freezing') : i18n.t('btn.unfreezing'))) return;
  try {
    await _postJson(`/api/project/${action}/${encodeURIComponent(S.selectedProject)}`);
    showToast(action === 'freeze' ? i18n.t('toast.projectBlocked') : i18n.t('toast.projectUnblocked'), 'ok');
    await refresh();
    _updateFreezeBtn();
    _populateProjectChips(S.projects || []);
  } catch(e) {
    showToast(String(e), 'err');
    openAlert('Error: ' + e);
  } finally {
    _btnDone(btn);
    _updateFreezeBtn();
  }
}

/** Pause/resume the selected project: queued tasks wait, running ones continue (POST /api/project/pause|resume). */
export async function toggleProjectPause() {
  if (S.selectedProject === 'all') return;
  const btn = document.getElementById('proj-pause-btn');
  const paused = !!(S.cfg && S.cfg[`project_paused_${S.selectedProject}`]);
  const action = paused ? 'resume' : 'pause';
  if (!_btnPending(btn, '…')) return;
  try {
    await _postJson(`/api/project/${action}/${encodeURIComponent(S.selectedProject)}`);
    if (S.cfg) {
      if (paused) delete S.cfg[`project_paused_${S.selectedProject}`];
      else S.cfg[`project_paused_${S.selectedProject}`] = 1;
    }
    showToast(i18n.t(paused ? 'toast.projectResumed' : 'toast.projectPaused'), 'ok');
    await refresh();
  } catch (e) {
    showToast(String(e.message || e), 'err');
  } finally {
    _btnDone(btn);
    _updateFreezeBtn();
  }
}

export function stopAllProject() {
  if (S.selectedProject === 'all') return;
  const btn = document.getElementById('proj-stopall-btn');
  if (!_btnPending(btn, i18n.t('btn.stopping'))) return;
  let settled = false;
  const release = () => {
    if (settled) return;
    settled = true;
    _btnDone(btn);
  };
  openConfirm(`Stop all running tasks in project "${S.selectedProject}"?`, async () => {
    try {
      const r = await fetch(`/api/project/stop-all/${encodeURIComponent(S.selectedProject)}`, {method:'POST'});
      const j = await r.json().catch(()=>({}));
      openAlert(i18n.t('alert.stoppedTasks', {n: j.stopped||0}));
      refresh();
    } catch(e) {
      openAlert('Error: ' + e);
    } finally {
      release();
    }
  }, {
    title: i18n.t('modal.stopProjectTitle'),
    confirmLabel: i18n.t('btn.stopAll'),
    onCancel: release,
  });
}

export async function workerSync(btn, workerId) {
  if (!_btnPending(btn, '…')) return;
  try {
    const r = await fetch(`/api/worker/sync/${encodeURIComponent(workerId)}`, {method:'POST'});
    const j = await r.json().catch(()=>({}));
    if (j.ok) {
      openAlert(`Sync queued for ${workerId}. The worker will git pull + uv sync on its next heartbeat (~30s).`, i18n.t('modal.syncQueuedTitle'));
    } else {
      openAlert(`Sync failed: ${j.error || 'unknown error'}`, i18n.t('modal.syncFailedTitle'));
    }
  } finally {
    _btnDone(btn);
  }
}

export function tqSetPriority(project, taskId, prio) {
  prio = Math.max(0, Math.min(100, parseInt(prio, 10) || 0));
  return _postJson(`/api/priority/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}/${prio}`)
    .then(() => {
      showToast((i18n.t('ctx.prioSet') || 'Priority set to {n}').replace('{n}', prio), 'ok');
      if (S.drawerTask && S.drawerTask.project === project && S.drawerTask.task_id === taskId) {
        S.drawerTask.priority = prio; tqPaintDrawerHead(); tqPaintDrawerBody();
      }
      S.forceQueueRender = true; refresh();
      _dagRefreshAfterAction();
    })
    .catch(err => showToast(String(err.message || err), 'err'));
}
export function tqPromptPriority(project, taskId, cur) {
  const v = window.prompt((i18n.t('ctx.promptPriority') || 'Priority (0–100)'), String(cur));
  if (v === null) return;
  tqSetPriority(project, taskId, v);
}
export function tqSetEnabled(project, taskId, on) {
  return _postJson(`/api/enable/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}?enabled=${on ? 1 : 0}`)
    .then(() => { refresh(); _dagRefreshAfterAction(); })
    .catch(err => showToast(String(err.message || err), 'err'));
}
