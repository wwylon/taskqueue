// Queue toolbar: saved filters, status chips, row selection, bulk actions, retry-all and CSV/JSON export.
// Uses app.js globals at call time (i18n, refresh, renderQueue, openConfirm/openAlert/showToast, ...).
import { S } from '../core/state.js';

// v3 queue: saved filters, bulk actions, export, retry-all, empty state
const _SAVED_FILTER_SEEDS = [
  { id: 'seed-failed-today', status: 'failed', search: '', project: 'all', today: true },
  { id: 'seed-running', status: 'running', search: '', project: 'all' },
  { id: 'seed-ci', status: 'all', search: 'ci', project: 'all' },
  { id: 'seed-ml', status: 'all', search: 'ml', project: 'all' },
];

export function tqPersistSavedFilters() {
  try { localStorage.setItem('tq_saved_filters', JSON.stringify(S.savedFilters)); } catch (e) {}
}
export function tqLoadSavedFilters() {
  try {
    const raw = localStorage.getItem('tq_saved_filters');
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) { S.savedFilters = parsed; return; }
    }
  } catch (e) {}
  S.savedFilters = _SAVED_FILTER_SEEDS.map(s => Object.assign({}, s));
  tqPersistSavedFilters();
}
export function tqFilterLabel(f) {
  if (f.id === 'seed-failed-today') return i18n.t('queue.presetFailedToday');
  if (f.id === 'seed-running') return i18n.t('queue.presetRunning');
  if (f.id === 'seed-ci') return i18n.t('queue.presetCi');
  if (f.id === 'seed-ml') return i18n.t('queue.presetMl');
  return f.name || f.id;
}
export function tqRenderSavedFilters() {
  const pop = document.getElementById('saved-filters-pop');
  if (!pop) return;
  if (!S.savedFilters.length) {
    pop.innerHTML = `<div class="sf-empty">${escHtml(i18n.t('queue.savedEmpty'))}</div>`;
    return;
  }
  pop.innerHTML = S.savedFilters.map(f => {
    const tip = f.today ? ` title="${escHtml(i18n.t('queue.failedTodayNote'))}"` : '';
    return `<div class="sf-item"${tip}>
      <button type="button" class="sf-apply" data-click="${tqAct([['tqApplySavedFilter', String(f.id)]])}">${escHtml(tqFilterLabel(f))}</button>
      <button type="button" class="sf-del" data-click="${tqAct([['tqDeleteSavedFilter', String(f.id), TQ_EVENT]])}" title="${escHtml(i18n.t('queue.deleteFilter'))}">×</button>
    </div>`;
  }).join('');
}
export function tqToggleSavedFilters() {
  const pop = document.getElementById('saved-filters-pop');
  const btn = document.getElementById('saved-filters-btn');
  if (!pop || !btn) return;
  if (!pop.hidden) { pop.hidden = true; return; }
  tqRenderSavedFilters();
  const r = btn.getBoundingClientRect();
  pop.hidden = false;
  pop.style.position = 'fixed';
  pop.style.zIndex = '80';
  pop.style.top = (r.bottom + 4) + 'px';
  pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 240)) + 'px';
  pop.style.right = 'auto';
}
export function tqSyncStatusChips() {
  document.querySelectorAll('#queue-filter-bar .filter-chip').forEach(b => {
    b.classList.toggle('active', b.dataset.status === S.queueStatusFilter);
  });
  document.querySelectorAll('#status-counts .sc').forEach(b => {
    b.classList.toggle('active', b.dataset.status === S.queueStatusFilter);
  });
}
export function tqApplySavedFilter(id) {
  const f = S.savedFilters.find(x => x.id === id);
  const pop = document.getElementById('saved-filters-pop');
  if (pop) pop.hidden = true;
  if (!f) return;
  S.savedToday = !!f.today;
  S.queueStatusFilter = f.status || 'all';
  S.forceQueueRender = true;
  S.queuePage = 0;
  try { localStorage.setItem('tq_status', S.queueStatusFilter); } catch (e) {}
  const search = document.getElementById('queue-search');
  if (search) search.value = f.search || '';
  _syncQueueSearchClear();
  tqSyncStatusChips();
  const proj = f.project || 'all';
  if (proj !== S.selectedProject) setProject(proj, { keepSearch: true });
  else refresh();
  if (!S.applyingUrl) _syncUrl({ replace: false });
}
export function tqDeleteSavedFilter(id, ev) {
  if (ev) ev.stopPropagation();
  S.savedFilters = S.savedFilters.filter(f => f.id !== id);
  tqPersistSavedFilters();
  tqRenderSavedFilters();
}
export function tqSaveCurrentFilter() {
  const searchEl = document.getElementById('queue-search');
  const search = searchEl ? searchEl.value.trim() : '';
  const suggested = [S.queueStatusFilter, search, S.selectedProject].filter(Boolean).join(' · ');
  const name = window.prompt(i18n.t('queue.saveFilterPrompt'), suggested);
  if (!name || !name.trim()) return;
  S.savedFilters.push({
    id: 'f-' + Date.now(),
    name: name.trim(),
    status: S.queueStatusFilter,
    search: search,
    project: S.selectedProject,
    today: !!S.savedToday,
  });
  tqPersistSavedFilters();
  showToast(i18n.t('queue.filterSaved'), 'ok');
}
export function tqClearQueueFilter() {
  S.savedToday = false;
  S.queueStatusFilter = 'all';
  S.queueActiveOnly = false;
  S.queueNewFailedOnly = false;
  S.forceQueueRender = true;
  S.queuePage = 0;
  S.tagFilter = null;
  S.groupFilter = null;
  try {
    localStorage.setItem('tq_status', 'all');
    localStorage.setItem('tq_tag', '');
    localStorage.setItem('tq_queue_active', '0');
  } catch (e) {}
  _resetQueueSearchForFilter();
  tqSyncStatusChips();
  if (typeof _updateTagChips === 'function') _updateTagChips();
  if (typeof _updateGroupChips === 'function') _updateGroupChips();
  if (!S.applyingUrl && typeof _syncUrl === 'function') _syncUrl({ replace: false });
  if (S.selectedProject !== 'all') {
    setProject('all');
  } else {
    if (typeof _populateProjectChips === 'function') _populateProjectChips(S.projects);
    tqRenderFilterSummary();
    refresh();
  }
}
/** Active filter chips + clear-all (project / active / mine / status / tag / group / search). */
export function tqRenderFilterSummary() {
  const el = document.getElementById('queue-filter-summary');
  if (!el) return;
  const chips = [];
  const add = (label, clearCall) => {
    chips.push(
      `<span class="fs-chip">${escHtml(label)}`
      + (clearCall
        ? ` <button type="button" class="cfg-btn" style="padding:0 4px;font-size:11px" data-click="${tqAct(clearCall)}" aria-label="Remove">×</button>`
        : '')
      + `</span>`
    );
  };
  if (S.mineOnly && S.operator) add(i18n.t('filter.mine') || 'mine', [['tqSetMineScope', false]]);
  if (S.queueActiveOnly) add(i18n.t('filter.active') || 'active', [['setProject', 'all']]);
  if (S.selectedProject && S.selectedProject !== 'all') {
    add((i18n.t('filter.labelProject') || 'project') + ' ' + S.selectedProject, [['setProject', 'all']]);
  }
  if (S.queueStatusFilter && S.queueStatusFilter !== 'all') {
    add((i18n.t('filter.labelStatus') || 'status') + ' ' + S.queueStatusFilter,
      [['setQueueFilter', 'all', null]]);
  }
  if (S.tagFilter) add('tag ' + S.tagFilter, [['toggleTagFilter', S.tagFilter]]);
  if (S.groupFilter) add('group ' + S.groupFilter, [['setGroupFilter', '']]);
  const search = ((document.getElementById('queue-search') || {}).value || S.queueSearch || '').trim();
  if (search) add('“' + search + '”', [['clearQueueSearch']]);
  if (!chips.length) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  el.innerHTML = chips.join('')
    + `<button type="button" class="cfg-btn fs-clear" data-click="${tqAct([['tqClearQueueFilter']])}">${escHtml(i18n.t('queue.clearAllFilters') || 'Clear all')}</button>`;
}
export function tqSyncQueueChrome() {
  const btn = document.getElementById('retry-all-failed-btn');
  if (btn) {
    const restart = _isRestartStatus(S.queueStatusFilter);
    const show = (S.queueStatusFilter === 'failed' || restart) && S.queueTotal > 0;
    btn.style.display = show ? '' : 'none';
    btn.textContent = restart
      ? i18n.t('queue.restartAll', { n: S.queueTotal })
      : i18n.t('queue.retryAllFailed', { n: S.queueTotal });
    btn.title = (!restart && S.savedToday) ? i18n.t('queue.retryAllQueryHint') : '';
  }
  tqRenderBulkBar();
  tqRenderFilterSummary();
}
export function tqSyncSelectAll(rows) {
  const all = document.getElementById('q-select-all');
  if (!all) return;
  const keys = (rows || []).map(t => _queueSelKey(t.project, t.task_id));
  const n = keys.filter(k => S.queueSelection.has(k)).length;
  all.checked = keys.length > 0 && n === keys.length;
  all.indeterminate = n > 0 && n < keys.length;
}
export function tqSetRowSelected(project, taskId, on) {
  const key = _queueSelKey(project, taskId);
  if (on) S.queueSelection.add(key);
  else S.queueSelection.delete(key);
  tqRenderBulkBar();
  tqSyncSelectAll(S.queueExportRows);
}
export function tqToggleSelectVisible(on) {
  (S.queueExportRows || []).forEach(t => {
    const key = _queueSelKey(t.project, t.task_id);
    if (on) S.queueSelection.add(key);
    else S.queueSelection.delete(key);
  });
  document.querySelectorAll('#queue .q-row-check').forEach(cb => { cb.checked = !!on; });
  tqRenderBulkBar();
  tqSyncSelectAll(S.queueExportRows);
}
export function tqClearSelection() {
  S.queueSelection.clear();
  document.querySelectorAll('#queue .q-row-check').forEach(cb => { cb.checked = false; });
  const all = document.getElementById('q-select-all');
  if (all) { all.checked = false; all.indeterminate = false; }
  tqRenderBulkBar();
}
export function tqRenderBulkBar() {
  const bar = document.getElementById('queue-bulk-bar');
  if (!bar) return;
  const n = S.queueSelection.size;
  if (!n) { bar.hidden = true; bar.innerHTML = ''; return; }
  bar.hidden = false;
  const dis = S.bulkBusy ? ' disabled' : '';
  // Offer the whole filtered result (all pages) once the visible page is selected.
  const more = S.queueTotal > n && S.queueTotal <= 5000
    ? `<button type="button" class="cfg-btn"${dis} data-click="${tqAct([['tqSelectAllMatching']])}">${escHtml(i18n.t('queue.selectAllMatching', {n: S.queueTotal}))}</button>` : '';
  bar.innerHTML = `<span class="bulk-count">${n}</span>${more}
    <button type="button" class="cfg-btn bulk-retry"${dis} data-click="${tqAct([['tqBulkAction', 'retry']])}">${escHtml(i18n.t('queue.bulkRetry', {n}))}</button>
    <button type="button" class="cfg-btn bulk-stop"${dis} data-click="${tqAct([['tqBulkAction', 'stop']])}">${escHtml(i18n.t('queue.bulkStop', {n}))}</button>
    <button type="button" class="cfg-btn"${dis} data-click="${tqAct([['tqBulkAction', 'requeue']])}">${escHtml(i18n.t('queue.bulkRequeue', {n}))}</button>
    <button type="button" class="cfg-btn" data-click="${tqAct([['tqClearSelection']])}">${escHtml(i18n.t('queue.bulkDeselect'))}</button>`;
}

/** Query string for the current Queue filters (project, status, search, tag, group). */
function _queueFilterQuery(limit) {
  const parts = [`limit=${limit}`, 'offset=0'];
  if (S.queueStatusFilter && S.queueStatusFilter !== 'all') parts.push(`status=${encodeURIComponent(S.queueStatusFilter)}`);
  if (S.selectedProject && S.selectedProject !== 'all') parts.push(`project=${encodeURIComponent(S.selectedProject)}`);
  const search = ((document.getElementById('queue-search') || {}).value || '').trim();
  if (search) parts.push(`search=${encodeURIComponent(search)}`);
  if (S.tagFilter) parts.push(`tag=${encodeURIComponent(S.tagFilter)}`);
  if (S.groupFilter) parts.push(`group=${encodeURIComponent(S.groupFilter)}`);
  return parts.join('&');
}

/** Select every task matching the current filters, across pages (server query, max 5000). */
export async function tqSelectAllMatching() {
  if (S.bulkBusy) return;
  const q = await _fetchJson('/api/queue?' + _queueFilterQuery(5000), 20000, true);
  if (!q) { showToast(i18n.t('sse.error'), 'err'); return; }
  (q.tasks || []).forEach(t => S.queueSelection.add(_queueSelKey(t.project, t.task_id)));
  S.bulkRows = Object.fromEntries((q.tasks || []).map(t => [_queueSelKey(t.project, t.task_id), t.status]));
  tqRenderBulkBar();
}

/** Selected keys as "project/task_id" (selection keys are NUL-separated). */
function _selectedTaskIds() {
  return [...S.queueSelection].map(k => {
    const sep = k.indexOf('\0');
    return [k.slice(0, sep), k.slice(sep + 1), k];
  });
}

// One request per bulk action: /api/tasks/bulk (stop) or /api/tasks/retry-batch (retry/requeue),
// instead of one POST per selected task. Confirm dialog shows the affected count first.
export async function tqBulkAction(kind) {
  if (S.bulkBusy || !S.queueSelection.size) return;
  const n = S.queueSelection.size;
  const labels = {
    stop: i18n.t('queue.bulkStopConfirm', { n }) || `Stop ${n} selected tasks?`,
    retry: i18n.t('queue.bulkRetryConfirm', { n }) || `Retry ${n} selected tasks?`,
    requeue: i18n.t('queue.bulkRequeueConfirm', { n }) || `Re-queue ${n} selected tasks?`,
  };
  const msg = labels[kind] || `Apply to ${n} tasks?`;
  const confirmLabel = kind === 'stop'
    ? (i18n.t('btn.stop') || 'Stop')
    : (i18n.t('btn.retry') || 'OK');
  openConfirm(msg, () => _tqBulkActionRun(kind), { confirmLabel });
}
async function _tqBulkActionRun(kind) {
  if (S.bulkBusy || !S.queueSelection.size) return;
  S.bulkBusy = true;
  tqRenderBulkBar();
  let ok = 0, failed = 0, skipped = 0;
  try {
    const sel = _selectedTaskIds();
    if (kind === 'stop') {
      const statusOf = key => {
        const row = (S.queue || []).find(t => _queueSelKey(t.project, t.task_id) === key);
        return row ? row.status : (S.bulkRows || {})[key];
      };
      const stoppable = sel.filter(([, , key]) => {
        const st = statusOf(key);
        return st === undefined || st === 'running' || st === 'queued';
      });
      skipped = sel.length - stoppable.length;
      if (stoppable.length) {
        const res = await _postJson('/api/tasks/bulk', { action: 'stop', tasks: stoppable.map(([p, t]) => `${p}/${t}`) });
        ok = (res.results || []).filter(r => r.ok).length;
        failed = (res.results || []).length - ok;
      }
    } else {
      const res = await _postJson('/api/tasks/retry-batch', { task_ids: sel.map(([p, t]) => `${p}/${t}`) });
      ok = (res.requeued || []).length;
      skipped = (res.skipped || []).length;
    }
  } catch (e) {
    failed = S.queueSelection.size - ok - skipped;
    if (e && e.message && !failed) failed = 1;
  } finally {
    S.bulkBusy = false;
  }
  showToast(i18n.t('queue.bulkDone', { ok, failed, skipped }), failed ? 'err' : 'ok');
  if (kind !== 'stop') tqClearSelection();
  else tqRenderBulkBar();
  refresh();
}
export async function tqRetryAllFailed() {
  const st = S.queueStatusFilter;
  const restart = _isRestartStatus(st);
  if ((st !== 'failed' && !restart) || !S.queueTotal || S.bulkBusy) return;
  const total = S.queueTotal;
  const run = async () => {
    S.bulkBusy = true;
    const btn = document.getElementById('retry-all-failed-btn');
    if (btn) btn.disabled = true;
    try {
      const proj = S.selectedProject !== 'all' ? S.selectedProject : '';
      const parts = [`status=${encodeURIComponent(st)}`, `limit=${Math.min(total, 5000)}`, 'offset=0'];
      if (proj) parts.push(`project=${encodeURIComponent(proj)}`);
      const search = ((document.getElementById('queue-search') || {}).value || '').trim();
      if (search) parts.push(`search=${encodeURIComponent(search)}`);
      if (S.tagFilter) parts.push(`tag=${encodeURIComponent(S.tagFilter)}`);
      if (S.groupFilter) parts.push(`group=${encodeURIComponent(S.groupFilter)}`);
      const q = await _fetchJson('/api/queue?' + parts.join('&'), 20000, true);
      if (!q) { showToast(i18n.t('sse.error'), 'err'); return; }
      const tasks = q.tasks || [];
      let ok = 0, failed = 0;
      if (tasks.length) {
        try {
          const res = await _postJson('/api/tasks/retry-batch', { task_ids: tasks.map(t => `${t.project}/${t.task_id}`) });
          ok = (res.requeued || []).length;
          failed = tasks.length - ok - (res.skipped || []).length;
        } catch (e) { failed = tasks.length; }
      }
      showToast(
        restart
          ? i18n.t('queue.restartAllDone', { ok, failed })
          : i18n.t('queue.retryAllDone', { ok, failed }),
        failed ? 'err' : 'ok'
      );
      refresh();
    } finally {
      S.bulkBusy = false;
      if (btn) btn.disabled = false;
    }
  };
  if (total > 500) {
    openConfirm(
      restart
        ? i18n.t('queue.restartAllConfirm', { n: total })
        : i18n.t('queue.retryAllConfirm', { n: total }),
      run,
      { confirmLabel: restart ? i18n.t('btn.restart') : i18n.t('btn.retry') }
    );
  } else {
    run();
  }
}
export function _csvCell(v) {
  let s = v == null ? '' : String(v);
  // Spreadsheet formula injection: neutralise cells that start with = + - @ tab or CR.
  if (/^[=+\-@\t\r]/.test(s) && !/^-?\d+(\.\d+)?$/.test(s)) s = "'" + s;
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
export function tqExportQueue(kind) {
  const rows = S.queueExportRows || [];
  const fields = ['project', 'task_id', 'status', 'priority', 'description', 'enqueued_at', 'started_at', 'finished_at', 'progress_pct'];
  let body, mime, ext;
  if (kind === 'json') {
    body = JSON.stringify(rows.map(t => {
      const o = {};
      fields.forEach(k => { o[k] = t[k] == null ? null : t[k]; });
      return o;
    }), null, 2);
    mime = 'application/json';
    ext = 'json';
  } else {
    body = fields.join(',') + '\n' + rows.map(t => fields.map(k => _csvCell(t[k])).join(',')).join('\n');
    mime = 'text/csv';
    ext = 'csv';
  }
  const blob = new Blob([body], { type: mime });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'queue.' + ext;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}
