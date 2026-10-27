// Queue tab: task table, mobile cards, search, filters, pagination, row flash and dependency jumps.
// Uses app.js globals at call time (i18n, badge, refresh, _syncUrl, _pageSize, _stripLive helpers, etc.).
import { S } from '../core/state.js';
import { projStyle } from '../util/projcolor.js';
import { escHtml, jsq, taskKey, qrowId, domIdPart, fmt, dur, isMobileLayout, onLayoutChange, progressHealthHtml } from '../util/format.js';

/** Must match queue table <th> count in index.html (incl. Started / Finished). */
export const QUEUE_COLS = 9;
export const QUEUE_FLASH_MS = 12000;

export function onQueueSearchInput() {
  S.queuePage = 0;
  _syncQueueSearchClear();
  const inp = document.getElementById('queue-search');
  if (inp) inp.classList.add('tq-searching');
  if (S.queueSearchDebounce) clearTimeout(S.queueSearchDebounce);
  S.queueSearchDebounce = setTimeout(() => {
    S.queueSearchDebounce = null;
    if (inp) inp.classList.remove('tq-searching');
    refresh();
    _syncUrl({ replace: true });
  }, 350);
}

export function _syncQueueSearchClear() {
  const inp = document.getElementById('queue-search');
  const btn = document.getElementById('queue-search-clear');
  if (!btn) return;
  const has = !!(inp && inp.value);
  btn.hidden = !has;
}

export function _resetQueueSearchForFilter() {
  if (S.queueSearchDebounce) {
    clearTimeout(S.queueSearchDebounce);
    S.queueSearchDebounce = null;
  }
  const inp = document.getElementById('queue-search');
  if (inp) {
    inp.value = '';
    inp.classList.remove('tq-searching');
  }
  _syncQueueSearchClear();
}

export function clearQueueSearch() {
  const inp = document.getElementById('queue-search');
  if (!inp) return;
  if (!inp.value) {
    _syncQueueSearchClear();
    return;
  }
  inp.value = '';
  _syncQueueSearchClear();
  if (S.queueSearchDebounce) {
    clearTimeout(S.queueSearchDebounce);
    S.queueSearchDebounce = null;
  }
  inp.classList.remove('tq-searching');
  S.queuePage = 0;
  refresh();
  _syncUrl({ replace: true });
  inp.focus();
}

export function setQueueFilter(status, btn, opts) {
  S.queueStatusFilter = status;
  S.queueNewFailedOnly = !!(opts && opts.newFailed);
  S.savedToday = false;
  S.forceQueueRender = true;
  S.queuePage = 0;
  _resetQueueSearchForFilter();
  try { localStorage.setItem('tq_status', status); } catch(e) {}
  tqSyncStatusChips();
  if (btn) btn.classList.add('active');
  refresh();
  if (!S.applyingUrl) _syncUrl({ replace: false });
}

export function setQueueProject(proj, btn) {
  S.queueProjectFilter = proj;
  S.queuePage = 0;
  S.historyPage = 0;
  S.historyLimit = S.historyPageSize;
  _resetQueueSearchForFilter();
  document.querySelectorAll('#queue-proj-bar .filter-chip').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  refresh();
}

export function sortQueue(field) {
  if (S.queueSort.field === field) S.queueSort.dir *= -1;
  else { S.queueSort.field = field; S.queueSort.dir = 1; }
  S.queuePage = 0;
  refresh();
}

export function renderQueue() {
  let rows = [...S.queue];
  if (S.globalWorkerFilter) {
    if (S.globalWorkerFilter === 'master') {
      rows = rows.filter(t => {
        if (t.status === 'running') {
          const run = (S.status.running || []).find(r => r.project === t.project && r.task_id === t.task_id);
          return run && !run.worker_id;
        }
        return true;
      });
    } else {
      rows = rows.filter(t => {
        if (t.status === 'running') {
          const run = (S.status.running || []).find(r => r.project === t.project && r.task_id === t.task_id);
          return run && run.worker_id === S.globalWorkerFilter;
        }
        // For non-running tasks, match against the last recorded worker_id from the run record
        return t.last_worker_id === S.globalWorkerFilter;
      });
    }
  }
  if (S.savedToday) {
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    const end = start.getTime() + 86400000;
    rows = rows.filter(t => {
      const ms = Date.parse(t.finished_at || '');
      return !isNaN(ms) && ms >= start.getTime() && ms < end;
    });
  }
  S.queueExportRows = rows;

  const heading = document.getElementById('queue-all-tasks-heading');
  if (heading) {
    const pageStart = S.queuePage * _pageSize() + 1;
    const pageEnd = Math.min((S.queuePage + 1) * _pageSize(), S.queueTotal);
    heading.textContent = S.queueTotal <= _pageSize()
      ? `All tasks (${S.queueTotal})`
      : `All tasks — ${pageStart}–${pageEnd} of ${S.queueTotal}`;
  }
  const todayNote = document.getElementById('queue-today-note');
  if (todayNote) {
    todayNote.hidden = !S.savedToday;
    todayNote.textContent = S.savedToday ? i18n.t('queue.failedTodayNote') : '';
  }
  tqSyncQueueChrome();

  _updateSortTh('q', S.queueSort.field, S.queueSort.dir);

  const tableWrap = document.querySelector('#queue .table-responsive');
  const emptyEl = document.getElementById('queue-empty');
  const mobileCards = document.getElementById('queue-mobile-cards');
  if (!rows.length) {
    const headingEmpty = document.getElementById('queue-all-tasks-heading');
    if (headingEmpty) headingEmpty.textContent = `All tasks (0 of ${S.queueTotal})`;
    if (tableWrap) tableWrap.style.display = 'none';
    if (mobileCards) { mobileCards.innerHTML = ''; mobileCards.style.display = 'none'; }
    if (emptyEl) emptyEl.hidden = false;
    const tbodyEmpty = document.getElementById('queue-tbody');
    if (tbodyEmpty) { tbodyEmpty.innerHTML = ''; tbodyEmpty._rowsSig = null; }
    _renderQueuePagination();
    tqSyncSelectAll(rows);
    return;
  }
  if (tableWrap) tableWrap.style.display = '';
  if (mobileCards) mobileCards.style.display = '';
  if (emptyEl) emptyEl.hidden = true;

  const tbody = document.getElementById('queue-tbody');
  const statusMap = {};
  (S.queue || []).forEach(t => { statusMap[`${t.project}/${t.task_id}`] = t.status; });
  (S.status.running || []).forEach(r => { statusMap[taskKey(r.project, r.task_id)] = 'running'; });
  const liveMap = Object.fromEntries((S.status.running || []).map(r => [taskKey(r.project, r.task_id), r]));
  // Build only the layout CSS shows (table on desktop, cards on mobile).
  const mobile = isMobileLayout();
  // Identical markup => keep the existing rows (no teardown, keeps selection/scroll/focus).
  const rowsHtml = mobile ? '' : rows.map(t => _buildQueueRow(t, statusMap)).join('');
  if (tbody._rowsSig !== rowsHtml || !tbody.firstChild) {
    tbody.innerHTML = '';
    if (rowsHtml) tbody.insertAdjacentHTML('beforeend', rowsHtml);
    tbody._rowsSig = rowsHtml;
  }

  // Mobile card view (liveMap carries running progress fields)
  if (mobileCards) {
    mobileCards.innerHTML = mobile ? rows.map(t => _buildQueueCard(t, liveMap, statusMap)).join('') : '';
  }

  // Do not overwrite S.prevQueueStr here — refresh() owns the identity key
  // (live fields stripped). Overwriting with raw S.queue forced perpetual rebuilds.
  _renderQueuePagination();
  _reapplyQueueFlash();
  tqSyncSelectAll(rows);
  tqSyncDrawerLive();
}

export function _renderQueuePagination() {
  const totalPages = Math.ceil(S.queueTotal / _pageSize());
  const html = totalPages <= 1 ? '' : (() => {
    const prev = S.queuePage > 0;
    const next = S.queuePage < totalPages - 1;
    const btnStyle = 'padding:4px 12px;border:1px solid var(--border);border-radius:4px;background:var(--bg3);color:var(--text);cursor:pointer;font-size:12px';
    const disStyle = 'padding:4px 12px;border:1px solid var(--border);border-radius:4px;background:var(--bg2);color:var(--muted);font-size:12px;cursor:default';
    const pageNums = [];
    let last = -1;
    const w = 3;
    for (let p = 0; p < totalPages; p++) {
      if (p === 0 || p === totalPages - 1 || (p >= S.queuePage - w && p <= S.queuePage + w)) {
        pageNums.push(p);
      }
    }
    const items = [];
    for (const p of pageNums) {
      if (last >= 0 && p > last + 1) items.push('…');
      const active = p === S.queuePage;
      items.push(`<button style="${active ? btnStyle + ';background:var(--accent);color:#fff;font-weight:700;border-color:var(--accent)' : btnStyle}" data-click="${tqAct([['goQueuePage', p]])}">${p + 1}</button>`);
      last = p;
    }
    return `<div style="display:flex;align-items:center;gap:6px;padding:8px 12px;font-size:12px;flex-wrap:wrap">
      <button style="${prev ? btnStyle : disStyle}" ${prev ? `data-click="${tqAct([['goQueuePage', S.queuePage - 1]])}"` : 'disabled'}>← prev</button>
      <span class="mob-pag-pages" style="display:contents">${items.join('')}</span>
      <button style="${next ? btnStyle : disStyle}" ${next ? `data-click="${tqAct([['goQueuePage', S.queuePage + 1]])}"` : 'disabled'}>next →</button>
      <span style="color:var(--muted);margin-left:4px">${S.queuePage + 1} / ${totalPages} · ${S.queueTotal}</span>
    </div>`;
  })();
  document.querySelectorAll('.queue-pagination-ctrl').forEach(el => { el.innerHTML = html; });
}

export function goQueuePage(p) {
  S.queuePage = p;
  refresh();
  const el = document.getElementById('queue-tbody');
  if (el) el.closest('.card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** Force button, or a disabled "Starting…" one while a confirmed Force waits for admission. */
export function _forceBtnHtml(t) {
  if (S.forceStarting && S.forceStarting[t.project + '/' + t.task_id]) {
    return `<button class="action-btn blue" disabled aria-busy="true"><span class="tq-spin" aria-hidden="true"></span> ${i18n.t('btn.starting')}</button>`;
  }
  return `<button class="action-btn blue" data-click="${tqAct([['forceTask', TQ_THIS, String(t.project), String(t.task_id)]])}">${i18n.t('btn.force')}</button>`;
}

/** Resolve dep statuses/counts for a task. statusMap is optional (drawer may pass {}). */
export function _depMeta(t, statusMap) {
  const deps = Array.isArray(t.depends_on) ? t.depends_on : [];
  const missingSet = new Set(Array.isArray(t.missing_deps) ? t.missing_deps : []);
  // Server-resolved status first: the loaded queue page does not hold deps that sit on other pages.
  const depSt = t.dep_status || {};
  const map = statusMap || {};
  const stOf = d => depSt[d] || map[`${t.project}/${d}`] || map[d] || '';
  const okCount = deps.filter(d => stOf(d) === 'completed').length;
  const deadCount = deps.filter(d => {
    if (missingSet.has(d)) return true;
    const st = stOf(d);
    return ['failed', 'stopped', 'interrupted', 'disabled'].includes(st);
  }).length;
  const countLabel = !deps.length ? '' : (deadCount
    ? i18n.t('task.depsCountDead', {ok: okCount, dead: deadCount, total: deps.length})
    : i18n.t('task.depsCount', {met: okCount, total: deps.length}));
  return { deps, missingSet, stOf, okCount, deadCount, countLabel };
}

/** Clickable status chips for every dependency (Details drawer). */
export function _depTagsHtml(t, statusMap) {
  const { deps, missingSet, stOf } = _depMeta(t, statusMap);
  if (!deps.length) return '';
  return deps.map(d => {
    const upStatus = stOf(d);
    let cls = 'dep-tag wait';
    let tip = upStatus || 'pending';
    if (missingSet.has(d)) {
      cls = 'dep-tag dead';
      tip = 'not found — broken dependency';
    } else if (upStatus === 'completed') {
      cls = 'dep-tag ok';
      tip = 'completed';
    } else if (['failed', 'stopped', 'interrupted', 'disabled'].includes(upStatus)) {
      cls = 'dep-tag dead';
      tip = `${upStatus} — dead dependency`;
    } else if (upStatus) {
      cls = 'dep-tag wait';
      tip = upStatus;
    }
    return `<button type="button" class="${cls}" title="${tip} — go to dependency" data-click="${tqAct([['gotoQueueDep', String(t.project), String(d)]])}">${escHtml(d)}</button>`;
  }).join('');
}

/** Queue row/card: compact count only — full chip list lives in the Details drawer. */
export function _queueDepHtml(t, statusMap) {
  const { deps, countLabel } = _depMeta(t, statusMap);
  if (!deps.length) return '';
  return `<div class="dep-line"><span class="dep-count">${countLabel}</span></div>`;
}

export function _queueSelKey(project, taskId) { return project + '\0' + taskId; }

export function _queueCheckHtml(t) {
  const on = S.queueSelection.has(_queueSelKey(t.project, t.task_id)) ? ' checked' : '';
  return `<input type="checkbox" class="q-row-check" data-project="${escHtml(t.project)}" data-task="${escHtml(t.task_id)}"${on} data-click="${tqAct([])}" data-click-stop>`;
}

export function _buildQueueRow(t, statusMap) {
    const key = taskKey(t.project, t.task_id);
    const actions = [];
    if (t.status === 'running' || t.status === 'queued') {
      actions.push(`<button class="action-btn" data-click="${tqAct([['stopTask', TQ_THIS, String(t.project), String(t.task_id)]])}">${i18n.t('btn.stop')}</button>`);
    }
    // F2: single retry path → POST /api/tasks/…/retry
    if (_isRestartStatus(t.status) || ['failed','completed'].includes(t.status)) {
      const label = _requeueLabel(t.status);
      actions.push(`<button class="action-btn" title="${label}" id="retry-btn-${domIdPart(t.project)}-${domIdPart(t.task_id)}" data-click="${tqAct([['retryTaskNew', String(t.project), String(t.task_id), TQ_THIS]])}">${label}</button>`);
    }
    if (t.status === 'queued') {
      actions.push(_forceBtnHtml(t));
    }
    if (t.status === 'completed') {
      if (t.processed_at) {
        actions.push(`<span title="Processed ${fmt(t.processed_at)}" style="font-size:10px;color:var(--muted);padding:2px 4px">✓ processed</span>`);
      } else {
        actions.push(`<button class="action-btn" title="Mark as processed by your project" data-click="${tqAct([['markProcessed', TQ_THIS, String(t.project), String(t.task_id)]])}">${i18n.t('btn.markDone')}</button>`);
      }
    }
    if (t.status === 'disabled') {
      actions.push(`<button class="action-btn green" data-click="${tqAct([['enableTask', String(t.project), String(t.task_id), TQ_THIS]])}">${i18n.t('btn.enable')}</button>`);
    }
    actions.push(`<button class="action-btn" data-click="${tqAct([['tqOpenDrawer', String(t.project), String(t.task_id)]])}">${i18n.t('btn.detail')}</button>`);
    // W3.5: YAML modal button (click = modal; was clipboard-only)
    actions.push(`<button class="action-btn" title="${i18n.t('task.yamlTitle')}" data-click="${tqAct([['openYamlModal', String(t.project), String(t.task_id)]])}">⎘</button>`);
    const histKey = `${t.project}/${t.task_id}`;
    const histExpanded = !!S.expandedHistory[histKey];
    const histRowId = `q-hist-row-${domIdPart(t.project)}-${domIdPart(t.task_id)}`;
    actions.push(`<button class="action-btn" id="q-histbtn-${domIdPart(t.project)}-${domIdPart(t.task_id)}" data-click="${tqAct([['toggleTaskHistory', String(t.project), String(t.task_id), String(histRowId)]])}" title="${i18n.t('task.historyTitle')}">${histExpanded ? i18n.t('btn.hideHistory') : i18n.t('btn.history')}</button>`);
    const rowId = `q-log-row-${t.run_id || escHtml(t.task_id)}`;
    const logOpen = t.run_id && !!S.expandedLogs[`q:${t.run_id}`];
    if (t.run_id) {
      actions.push(`<button class="action-btn blue" id="q-logbtn-${t.run_id}" data-click="${tqAct([['toggleQueueLog', t.run_id, String(rowId), t.status==='running']])}">${logOpen ? i18n.t('btn.hideLog') : i18n.t('btn.log')}</button>`);
    }
    const taskTags = Array.isArray(t.tags) ? t.tags : [];
    const tagChipsHtml = taskTags.length ? `<div style="display:inline-flex;flex-wrap:wrap;gap:2px;margin-left:4px;vertical-align:middle">${taskTags.map(tag =>
      `<button type="button" data-click="${tqAct([['toggleTagFilter', String(tag)]])}" style="font-size:9px;padding:1px 5px;border-radius:8px;border:1px solid var(--accent);color:var(--accent);background:transparent;cursor:pointer;line-height:1.4">${escHtml(tag)}</button>`
    ).join('')}</div>` : '';
    const depHtml = _queueDepHtml(t, statusMap);

    const logRowHtml = t.run_id ? `<tr class="inline-log-row${S.expandedLogs[`q:${t.run_id}`]?' open':''}" id="${rowId}">
      <td colspan="${QUEUE_COLS}" class="inline-log-wrap">
        <div class="inline-log-toolbar">
          <span>run #${t.run_id} · ${escHtml(t.project)}/${escHtml(t.task_id)}</span>
          <label style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted);cursor:pointer">
            <input type="checkbox" id="q-log-follow-${t.run_id}" checked> ${i18n.t('log.follow')}
          </label>
          <button class="action-btn" data-click="${tqAct([['_fetchLog', t.run_id, `q-log-pre-${t.run_id}`]])}">${i18n.t('btn.refresh')}</button>
          <button class="action-btn" data-click="${tqAct([['tailQueueLog', t.run_id]])}">${i18n.t('btn.tail')}</button>
          <button class="action-btn" data-click="${tqAct([['copyLogTail', t.run_id, `q-log-pre-${t.run_id}`, TQ_THIS]])}" title="${i18n.t('log.copyTitle')}">${i18n.t('btn.copyLog')}</button>
          <button class="action-btn" data-click="${tqAct([['toggleQueueLog', t.run_id, String(rowId), t.status==='running']])}" style="margin-left:auto">×</button>
          <div class="log-search-row">
            <input type="text" class="log-search-input" id="log-search-inp-q-log-pre-${t.run_id}" placeholder="${i18n.t('log.searchPlaceholder')}" data-input="${tqAct([['searchLog', TQ_THIS, t.run_id, `q-log-pre-${t.run_id}`]])}" style="padding:2px 6px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:11px">
            <button class="action-btn" data-click="${tqAct([['logSearchNav', `q-log-pre-${t.run_id}`, -1]])}" title="${i18n.t('log.prevMatch')}">↑</button>
            <button class="action-btn" data-click="${tqAct([['logSearchNav', `q-log-pre-${t.run_id}`, 1]])}" title="${i18n.t('log.nextMatch')}">↓</button>
            <span id="log-search-count-q-log-pre-${t.run_id}" style="font-size:10px;color:var(--muted)"></span>
          </div>
        </div>
        <pre class="inline-pre" id="q-log-pre-${t.run_id}">${S.expandedLogs[`q:${t.run_id}`] ? _ansiToHtml(S.expandedLogs[`q:${t.run_id}`].content || 'loading…') : 'loading…'}</pre>
      </td>
    </tr>` : '';

    const histRowHtml = `<tr class="inline-log-row${histExpanded ? ' open' : ''}" id="${histRowId}">
      <td colspan="${QUEUE_COLS}" class="inline-log-wrap">
        <div class="inline-log-toolbar">
          <span style="font-weight:600">Run history · ${escHtml(t.project)}/${escHtml(t.task_id)}</span>
          <button class="action-btn" data-click="${tqAct([['toggleTaskHistory', String(t.project), String(t.task_id), String(histRowId)]])}" style="margin-left:auto">×</button>
        </div>
        <div id="q-hist-content-${domIdPart(t.project)}-${domIdPart(t.task_id)}" style="padding:6px;font-size:11px">
          ${histExpanded ? (S.expandedHistory[histKey] || '<em>loading…</em>') : '<em>loading…</em>'}
        </div>
      </td>
    </tr>`;

    const activeRun = (S.status.running || []).find(r => r.project === t.project && r.task_id === t.task_id);
    // Show worker badge: purple for workers, grey for master; use last_worker_id for completed/failed tasks
    const _workerLabel = activeRun ? (activeRun.worker_id || null) : (t.last_worker_id || null);
    const workerTag = _workerLabel ? `<span class="worker-tag" style="background:${_workerLabel !== null ? '#4F46E5' : '#6B7280'};color:#fff;font-size:9px;padding:1px 4px;border-radius:3px;font-weight:bold;margin-left:4px" title="ran on ${escHtml(_workerLabel)}">@${escHtml(_workerLabel)}</span>` : '';
    // J2: worker health chip for remote tasks
    let workerHealthChip = '';
    if (t.status === 'running' && activeRun && activeRun.worker_id) {
      const ageSec = activeRun.worker_last_seen_age_sec;
      if (ageSec != null) {
        const wColor = ageSec < 30 ? '#22c55e' : ageSec < 120 ? '#f59e0b' : '#ef4444';
        const truncId = activeRun.worker_id.length > 12 ? activeRun.worker_id.slice(0,12)+'…' : activeRun.worker_id;
        workerHealthChip = `<span title="last seen ${ageSec.toFixed(0)}s ago" style="background:var(--bg3);border:1px solid ${wColor};color:${wColor};font-size:9px;padding:1px 5px;border-radius:8px;margin-left:4px">${escHtml(truncId)}</span>`;
      }
    }
    const qrowIdAttr = escHtml(qrowId(t.project, t.task_id));
    const gpuBadge = _gpuBadgeHtml(t);
    const logSrc = (activeRun && activeRun.log) ? activeRun.log : t.log;
    const logChips = t.run_id ? _logChipsHtml(
      { log: logSrc, run_id: t.run_id, project: t.project, task_id: t.task_id },
      { panel: 'q', runId: t.run_id, rowId, project: t.project, taskId: t.task_id, isRunning: t.status === 'running' }
    ) : '';

    // N2: priority cell — click to edit
    const prioCell = `<td class="mob-hide"><span class="prio-span" style="cursor:pointer;padding:2px 6px;border-radius:5px;background:var(--bg3);border:1px solid var(--border);font-size:12px" title="${i18n.t('task.editPriority')}" data-click="${tqAct([['startPrioEdit', TQ_THIS, String(t.project), String(t.task_id), t.priority]])}">${t.priority}</span></td>`;

    return `<tr id="${qrowIdAttr}" data-project="${escHtml(t.project)}" data-task="${escHtml(t.task_id)}"${S.kbdTaskKey === _queueSelKey(t.project, t.task_id) ? ' class="qrow-kbd"' : ''}>
      <td class="q-check">${_queueCheckHtml(t)}</td>
      ${prioCell}
      <td class="task-cell" style="max-width:320px;cursor:pointer" title="${escHtml(t.project)}/${escHtml(t.task_id)}" data-click="${tqAct([['tqOnTaskCellClick', TQ_EVENT, String(t.project), String(t.task_id)]])}"><span class="proj-tag" style="${projStyle(t.project)}">${escHtml(t.project)}</span>${_taskCopyIconBtn(t.project, t.task_id)}<span class="task-id-mono">${escHtml(t.task_id)}</span>${gpuBadge}${workerTag}${workerHealthChip}${logChips ? ' ' + logChips : ''}${depHtml}</td>
      <td>${badge(t.status, t.last_error_code, t.block_reason)}${waitReasonHtml(t)}${
        // W3.7: progress bar + ETA in queue table
        t.status === 'running' && t.progress_pct != null
          ? `<div class="pbar-wrap pbar-3"><div class="pbar" data-q-pbar style="width:${t.progress_pct.toFixed(1)}%"></div></div>`
            + `<span data-q-eta style="font-size:10px;color:var(--muted)">${
                _etaRemainingSec(t, t.elapsed_sec, t.progress_pct) != null
                  ? `${t.progress_pct.toFixed(0)}% · ETA ${dur(_etaRemainingSec(t, t.elapsed_sec, t.progress_pct))}`
                  : `${t.progress_pct.toFixed(0)}%`
              }</span>`
            + progressHealthHtml(t.progress_health)
          : (_progressPct(t) != null ? _lastRunProgressHtml(t, _progressPct(t)) : '')
      }</td>
      <td class="mob-hide">${fmt(t.started_at)}</td>
      <td class="mob-hide">${t.finished_at ? fmt(t.finished_at) : (t.status==='running' ? '<span style="color:var(--muted)">—</span>' : '')}</td>
      <td class="mob-hide">${res(t.resources, t.peak_ram_gib, t.peak_vram_gib)}${t.run_id ? `<button class="action-btn" data-click="${tqAct([['showResourceModal', t.run_id]])}" style="margin-top:4px;display:block;font-size:10px">📊 metrics</button>` : ''}</td>
      <td class="mob-hide desc-cell" style="color:var(--text2)">${escHtml(t.description||'')}</td>
      <td data-click="${tqAct([])}" data-click-stop>${actions.join('')}</td>
    </tr>${logRowHtml}${histRowHtml}`;
}

export function _progressPct(t) {
  if (!t || t.progress_pct == null || t.status === 'completed') return null;
  const pct = Number(t.progress_pct);
  if (!isFinite(pct)) return null;
  if (t.status === 'running') return pct;
  if (t.status === 'queued' || t.status === 'disabled') return pct > 0 && pct < 100 ? pct : null;
  return pct;
}

export function _lastRunProgressHtml(t, pct) {
  return `<div class="pbar-wrap pbar-3 pbar-last" title="${escHtml(i18n.t('progress.lastRunTitle'))}"><div class="pbar pbar-${escHtml(t.status)}" style="width:${Math.min(pct, 100).toFixed(1)}%"></div></div>`
    + `<span style="font-size:10px;color:var(--muted)">${pct.toFixed(0)}% · ${escHtml(i18n.t('progress.lastRun'))}</span>`;
}

export function _buildQueueCard(t, liveMap, statusMap) {
  const live = (liveMap || {})[taskKey(t.project, t.task_id)] || {};
  const isRunning = t.status === 'running';
  const merged = isRunning && live.run_id ? Object.assign({}, t, live) : t;
  const shownPct = _progressPct(merged);
  const pct = shownPct != null ? shownPct : 0;
  const pctKnown = shownPct != null;
  const frozenThresh = parseFloat(S.cfg.frozen_threshold_sec || 600);
  const idleSec = live.idle_sec || 0;
  const isFrozen = isRunning && idleSec > frozenThresh;
  const lastLine = live.last_log_line || t.last_log_line || '';
  const workerLabel = isRunning ? (live.worker_id || 'master') : (t.last_worker_id || null);
  let eta = '';
  const etaRem = pctKnown
    ? _etaRemainingSec(Object.assign({}, merged, live), live.elapsed_sec || merged.elapsed_sec, pct) : null;
  if (etaRem != null) eta = ` · ETA ~${dur(etaRem)}`;
  const elapsed = (live.elapsed_sec || merged.elapsed_sec) ? dur(live.elapsed_sec || merged.elapsed_sec) : '';
  const lastTs = isRunning && idleSec > 5 ? ` · last output ${dur(idleSec)} ago` : '';
  const taskTags = Array.isArray(t.tags) && t.tags.length
    ? `<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:6px">${t.tags.map(tag =>
        `<button data-click="${tqAct([['toggleTagFilter', String(tag)]])}" style="font-size:10px;padding:1px 6px;border-radius:8px;border:1px solid var(--accent);color:var(--accent);background:transparent;cursor:pointer">${escHtml(tag)}</button>`
      ).join('')}</div>` : '';
  const cardDepHtml = _queueDepHtml(t, statusMap || {});

  const cardHistKey = `${t.project}/${t.task_id}`;
  const cardHistOpen = !!S.expandedHistory[cardHistKey];
  const cardLogKey = `qc:${t.run_id}`;
  const cardLogOpen = t.run_id && !!S.expandedLogs[cardLogKey];

  // Action buttons — same logic as _buildQueueRow
  const acts = [];
  if (t.status === 'running' || t.status === 'queued')
    acts.push(`<button class="stop-btn" data-click="${tqAct([['stopTask', TQ_THIS, String(t.project), String(t.task_id)]])}">${i18n.t('btn.stop')}</button>`);
  if (_isRestartStatus(t.status) || ['failed','completed'].includes(t.status))
    acts.push(`<button class="action-btn" data-click="${tqAct([['retryTaskNew', String(t.project), String(t.task_id), TQ_THIS]])}">${_requeueLabel(t.status)}</button>`);
  if (t.status === 'queued')
    acts.push(_forceBtnHtml(t));
  if (t.status === 'completed') {
    if (t.processed_at)
      acts.push(`<span style="font-size:10px;color:var(--muted)">✓ processed</span>`);
    else
      acts.push(`<button class="action-btn" data-click="${tqAct([['markProcessed', TQ_THIS, String(t.project), String(t.task_id)]])}">${i18n.t('btn.markDone')}</button>`);
  }
  if (t.status === 'disabled')
    acts.push(`<button class="action-btn green" data-click="${tqAct([['enableTask', String(t.project), String(t.task_id), TQ_THIS]])}">${i18n.t('btn.enable')}</button>`);
  acts.push(`<button class="action-btn" data-click="${tqAct([['tqOpenDrawer', String(t.project), String(t.task_id)]])}">${i18n.t('btn.detail')}</button>`);
  acts.push(`<button class="action-btn" data-click="${tqAct([['openYamlModal', String(t.project), String(t.task_id)]])}">⎘</button>`);
  acts.push(`<button class="action-btn" id="qc-histbtn-${domIdPart(t.project)}-${domIdPart(t.task_id)}" data-click="${tqAct([['toggleCardHistory', String(t.project), String(t.task_id)]])}">${cardHistOpen ? i18n.t('btn.hideHistory') : i18n.t('btn.history')}</button>`);
  if (t.run_id) {
    acts.push(`<button class="log-btn" id="qc-logbtn-${t.run_id}" data-click="${tqAct([['toggleCardLog', t.run_id, String(t.project), String(t.task_id), t.status==='running']])}">${cardLogOpen ? i18n.t('btn.hideLog') : i18n.t('btn.log')}</button>`);
    acts.push(`<button class="log-btn" data-click="${tqAct([['showResourceModal', t.run_id]])}" title="${i18n.t('task.metricsTitle')}">📊</button>`);
  }

  // Embedded log panel (card-scoped IDs: qc- prefix)
  const cardLogHtml = t.run_id ? `<div id="qc-log-wrap-${t.run_id}" style="display:${cardLogOpen?'block':'none'};margin-top:8px">
    <div class="inline-log-toolbar">
      <span>run #${t.run_id} · ${escHtml(t.project)}/${escHtml(t.task_id)}</span>
      <button class="action-btn" data-click="${tqAct([['_fetchLog', t.run_id, `qc-log-pre-${t.run_id}`]])}">${i18n.t('btn.refresh')}</button>
      <button class="action-btn" data-click="${tqAct([['copyLogTail', t.run_id, `qc-log-pre-${t.run_id}`, TQ_THIS]])}" title="${i18n.t('log.copyTitle')}">${i18n.t('btn.copyLog')}</button>
      <button class="action-btn" data-click="${tqAct([['toggleCardLog', t.run_id, String(t.project), String(t.task_id), false]])}" style="margin-left:auto">×</button>
      <div class="log-search-row">
        <input type="text" class="log-search-input" id="log-search-inp-qc-log-pre-${t.run_id}" placeholder="${i18n.t('log.searchPlaceholder')}" data-input="${tqAct([['searchLog', TQ_THIS, t.run_id, `qc-log-pre-${t.run_id}`]])}" style="padding:2px 6px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:11px">
        <button class="action-btn" data-click="${tqAct([['logSearchNav', `qc-log-pre-${t.run_id}`, -1]])}">↑</button>
        <button class="action-btn" data-click="${tqAct([['logSearchNav', `qc-log-pre-${t.run_id}`, 1]])}">↓</button>
        <span id="log-search-count-qc-log-pre-${t.run_id}" style="font-size:10px;color:var(--muted)"></span>
      </div>
    </div>
    <pre class="inline-pre" id="qc-log-pre-${t.run_id}">${cardLogOpen ? _ansiToHtml(S.expandedLogs[cardLogKey].content || 'loading…') : ''}</pre>
  </div>` : '';

  // Embedded history panel
  const cardHistHtml = `<div id="qc-hist-wrap-${domIdPart(t.project)}-${domIdPart(t.task_id)}" style="display:${cardHistOpen?'block':'none'};margin-top:8px">
    <div class="inline-log-toolbar">
      <span style="font-weight:600">Run history</span>
      <button class="action-btn" data-click="${tqAct([['toggleCardHistory', String(t.project), String(t.task_id)]])}" style="margin-left:auto">×</button>
    </div>
    <div id="qc-hist-content-${domIdPart(t.project)}-${domIdPart(t.task_id)}" style="font-size:11px;padding:4px 0">
      ${cardHistOpen ? (S.expandedHistory[cardHistKey] || '<em>loading…</em>') : ''}
    </div>
  </div>`;

  if (isRunning) {
    return `<div class="run-card${isFrozen ? ' frozen' : ''}" data-run-id="${merged.run_id || ''}">
      <div class="run-top">
        ${_queueCheckHtml(t)}
        ${merged.run_id ? `<span class="run-num" title="run_id">#${merged.run_id}</span>` : ''}
        ${_taskCopyIconBtn(t.project, t.task_id)}
        <span class="run-proj">${escHtml(t.project)}</span>
        <span class="run-id">${escHtml(t.task_id)}</span>
        ${_gpuBadgeHtml(t)}
        <span class="run-worker" style="background:${live.worker_id ? '#4F46E5' : '#6B7280'};color:#fff;font-size:11px;padding:2px 6px;border-radius:4px;font-weight:bold;margin-left:4px">${escHtml(workerLabel)}</span>
        ${elapsed ? `<span class="run-elapsed">${elapsed}</span>` : ''}
        ${pctKnown ? `<span class="run-eta">${pct.toFixed(0)}%${isRunning ? eta : ' · ' + escHtml(i18n.t('progress.lastRun'))}</span>` : ''}
        ${isFrozen ? `<span class="frozen-badge">FROZEN ${dur(idleSec)} no output</span>` : ''}
        <div class="run-actions">${acts.join('')}</div>
      </div>
      <div class="pbar-wrap${isRunning ? '' : ' pbar-last'}"><div class="pbar${isRunning ? '' : ' pbar-' + escHtml(t.status)}" style="width:${Math.min(pct, 100)}%"></div></div>
      <div class="run-log">${lastLine ? escHtml(lastLine) : i18n.t('log.noOutput')}</div>
      ${lastTs ? `<div class="run-last-ts">${lastTs}</div>` : ''}
      ${merged.run_id ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">${_usageHtml(merged, { panel: 'qc', project: t.project, taskId: t.task_id, isRunning })}</div>` : ''}
      ${taskTags}
      ${cardDepHtml}
      ${cardLogHtml}${cardHistHtml}
    </div>`;
  }

  return `<div class="run-card">
    <div class="run-top">
      ${_queueCheckHtml(t)}
      ${_taskCopyIconBtn(t.project, t.task_id)}
      <span class="run-proj">${escHtml(t.project)}</span>
      <span class="run-id">${escHtml(t.task_id)}</span>
      ${_gpuBadgeHtml(t)}
      ${workerLabel ? `<span class="run-worker" style="background:#6B7280;color:#fff;font-size:11px;padding:2px 6px;border-radius:4px;font-weight:bold">${escHtml(workerLabel)}</span>` : ''}
      <span style="margin-left:auto">${badge(t.status, t.last_error_code, t.block_reason)}${waitReasonHtml(t)}</span>
    </div>
    <div class="pbar-wrap"><div class="pbar" style="width:${Math.min(pct,100)}%"></div></div>
    ${lastLine ? `<div class="run-log">${escHtml(lastLine)}</div>` : ''}
    ${t.description ? `<div class="desc-cell" style="font-size:11px;color:var(--text2);margin-top:3px">${escHtml(t.description)}</div>` : ''}
    ${res(t.resources, t.peak_ram_gib, t.peak_vram_gib) ? `<div style="font-size:11px;color:var(--muted);margin-top:2px">${res(t.resources, t.peak_ram_gib, t.peak_vram_gib)}</div>` : ''}
    ${t.started_at ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">${t.enqueued_at ? 'q ' + fmt(t.enqueued_at) + ' · ' : ''}started ${fmt(t.started_at)}</div>` : (t.enqueued_at ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">queued ${fmt(t.enqueued_at)}</div>` : '')}
    ${taskTags}
    ${cardDepHtml}
    <div class="run-actions" style="margin-top:8px;margin-left:0;flex-wrap:wrap;gap:4px">${acts.join('')}</div>
    ${cardLogHtml}${cardHistHtml}
  </div>`;
}

export function _flushQueueBatch(tbody) {
  if (_queueSentinelObs) { _queueSentinelObs.disconnect(); _queueSentinelObs = null; }
  const sentinel = tbody.querySelector('tr.q-sentinel');
  if (sentinel) sentinel.remove();

  const statusMap = {};
  _queueRenderRows.forEach(t => { statusMap[`${t.project}/${t.task_id}`] = t.status; });

  const batch = _queueRenderRows.slice(_queueRenderOffset, _queueRenderOffset + _QUEUE_BATCH);
  if (!batch.length) return;
  const html = batch.map(t => _buildQueueRow(t, statusMap)).join('');
  tbody.insertAdjacentHTML('beforeend', html);
  _queueRenderOffset += batch.length;

  if (_queueRenderOffset < _queueRenderRows.length) {
    const remaining = _queueRenderRows.length - _queueRenderOffset;
    const s = document.createElement('tr');
    s.className = 'q-sentinel';
    s.innerHTML = `<td colspan="${QUEUE_COLS}" style="text-align:center;padding:6px;color:var(--muted);font-size:11px">${i18n.t('queue.moreRows', {n: remaining})}</td>`;
    tbody.appendChild(s);
    _queueSentinelObs = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) _flushQueueBatch(tbody);
    }, { rootMargin: '200px' });
    _queueSentinelObs.observe(s);
  }
  _reapplyQueueFlash();
}

export function _flashQueueRow(project, taskId, { smooth = true } = {}) {
  S.queueFlashKey = project + '\0' + taskId;
  if (S.queueFlashTimer) clearTimeout(S.queueFlashTimer);
  S.queueFlashTimer = setTimeout(() => {
    S.queueFlashKey = null;
    S.queueFlashTimer = null;
    const el = document.getElementById(qrowId(project, taskId));
    if (el) el.classList.remove('qrow-flash');
  }, QUEUE_FLASH_MS);
  const el = document.getElementById(qrowId(project, taskId));
  if (!el) return;
  el.classList.add('qrow-flash');
  el.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'center' });
}

export function _reapplyQueueFlash() {
  if (!S.queueFlashKey) return;
  const sep = S.queueFlashKey.indexOf('\0');
  if (sep < 0) return;
  const el = document.getElementById(qrowId(S.queueFlashKey.slice(0, sep), S.queueFlashKey.slice(sep + 1)));
  if (el) el.classList.add('qrow-flash');
}

export function gotoQueueDep(project, depTaskId) {
  let depId = depTaskId;
  let depProj = project;
  if (depTaskId.includes('/')) {
    const parts = depTaskId.split('/');
    depProj = parts[0];
    depId = parts.slice(1).join('/');
  }
  const search = document.getElementById('queue-search');
  if (search) search.value = depId;
  S.queueStatusFilter = 'all';
  S.savedToday = false;
  S.forceQueueRender = true;
  S.groupFilter = null;
  S.queuePage = 0;
  document.querySelectorAll('#queue-filter-bar .filter-chip').forEach(b => b.classList.remove('active'));
  const allBtn = document.querySelector('#queue-filter-bar .filter-chip');
  if (allBtn) allBtn.classList.add('active');
  if (depProj && depProj !== S.selectedProject) {
    setProject(depProj, { keepSearch: true });
  } else {
    refresh().then(() => _flashQueueRow(depProj, depId));
    if (!S.applyingUrl) _syncUrl({ replace: false });
  }
}

export function gotoQueueProject(project) {
  if (!project) return;
  setProject(project);
  const qbtn = Array.from(document.querySelectorAll('nav button')).find(b =>
    b.getAttribute('data-i18n') === 'nav.queue' || b.textContent.trim().toLowerCase() === 'queue'
  );
  if (qbtn) tab('queue', qbtn);
}

onLayoutChange(() => { try { renderQueue(); } catch (_) {} });
