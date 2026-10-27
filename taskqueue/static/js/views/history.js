// History tab: run table, mobile cards, filters, search and per-task run history panels.
// Uses app.js globals at call time: tqAct, TQ_THIS, i18n, badge, refresh, _updateSortTh, _fetchLog, _ansiToHtml,
// _taskCopyIconBtn, _isRestartStatus, _requeueLabel, _loadTaskRunHistoryHtml.
import { S } from '../core/state.js';
import { escHtml, fmt, dur, domIdPart, isMobileLayout, onLayoutChange } from '../util/format.js';
import { projStyle } from '../util/projcolor.js';
import { fetchJson } from '../core/api.js';

export const HISTORY_COLS = 7;

export function onHistorySearchInput() {
  S.historyLimit = S.historyPageSize;
  _syncHistorySearchClear();
  const inp = document.getElementById('history-search');
  if (inp) inp.classList.add('tq-searching');
  if (S.historySearchDebounce) clearTimeout(S.historySearchDebounce);
  S.historySearchDebounce = setTimeout(() => {
    S.historySearchDebounce = null;
    if (inp) inp.classList.remove('tq-searching');
    refresh();
  }, 350);
}

export function _syncHistorySearchClear() {
  const inp = document.getElementById('history-search');
  const btn = document.getElementById('history-search-clear');
  if (!btn) return;
  btn.hidden = !(inp && inp.value);
}

export function clearHistorySearch() {
  const inp = document.getElementById('history-search');
  if (!inp) return;
  if (!inp.value) {
    _syncHistorySearchClear();
    return;
  }
  inp.value = '';
  _syncHistorySearchClear();
  if (S.historySearchDebounce) {
    clearTimeout(S.historySearchDebounce);
    S.historySearchDebounce = null;
  }
  inp.classList.remove('tq-searching');
  S.historyLimit = S.historyPageSize;
  refresh();
  inp.focus();
}

export function setHistoryFilter(status, btn) {
  S.historyStatusFilter = status;
  document.querySelectorAll('#history-filter-bar .filter-chip').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderHistory();
}

export function sortHistory(field) {
  if (S.historySort.field === field) S.historySort.dir *= -1;
  else { S.historySort.field = field; S.historySort.dir = -1; }
  _updateSortTh('h', S.historySort.field, S.historySort.dir);
  renderHistory();
}

// The server caps one history page at 1000 runs; past that, search narrows instead of paging.
export const HISTORY_MAX_LIMIT = 1000;

export function loadMoreHistory() {
  S.historyLimit = Math.min(HISTORY_MAX_LIMIT, S.historyLimit + S.historyPageSize);
  refresh();
}

export function _renderHistoryMore() {
  const hpag = document.getElementById('history-pagination');
  if (!hpag) return;
  const more = S.history.length < S.historyTotal;
  if (!more) {
    hpag.textContent = S.historyTotal ? `${S.historyTotal} run${S.historyTotal !== 1 ? 's' : ''}` : '';
    return;
  }
  if (S.historyLimit >= HISTORY_MAX_LIMIT) {
    hpag.innerHTML = `<span style="font-size:12px;color:var(--muted)">${S.history.length} / ${S.historyTotal} · ${escHtml(i18n.t('history.capHint'))}</span>`;
    return;
  }
  hpag.innerHTML = `<span style="font-size:12px;color:var(--muted);margin-right:8px">${S.history.length} / ${S.historyTotal}</span><button type="button" class="action-btn" data-click="${tqAct([['loadMoreHistory']])}">${escHtml(i18n.t('history.loadMore'))}</button>`;
}

export function _historyDuration(r) {
  if (r.started_at && r.finished_at) {
    const sec = (new Date(r.finished_at) - new Date(r.started_at)) / 1000;
    return dur(sec);
  }
  if (r.started_at && r.status === 'running') {
    const sec = (Date.now() - new Date(r.started_at)) / 1000;
    return dur(sec) + ' ' + i18n.t('history.durationRunning');
  }
  return '—';
}

export function _buildHistoryCard(r) {
  const durStr = _historyDuration(r);
  const logKey = `h:${r.run_id}`;
  const logOpen = !!S.expandedLogs[logKey];
  const peak = (r.peak_ram_gib || r.peak_vram_gib)
    ? `<div style="font-size:10px;color:var(--muted);margin-top:4px">peak: ${r.peak_ram_gib ? r.peak_ram_gib.toFixed(1) + 'G ram' : ''}${r.peak_vram_gib ? ' / ' + r.peak_vram_gib.toFixed(1) + 'G vram' : ''}</div>`
    : '';
  const meta = [
    r.started_at ? fmt(r.started_at) : null,
    durStr !== '—' ? durStr : null,
    r.exit_code != null ? `exit ${r.exit_code}` : null,
  ].filter(Boolean).join(' · ');
  const logHtml = `<div id="hc-log-wrap-${r.run_id}" style="display:${logOpen ? 'block' : 'none'};margin-top:8px">
    <div class="inline-log-toolbar">
      <span>run #${r.run_id} · ${escHtml(r.project || '')}/${escHtml(r.task_id)}</span>
      <button class="action-btn" data-click="${tqAct([['_fetchLog', r.run_id, `hc-log-pre-${r.run_id}`]])}">${i18n.t('btn.refresh')}</button>
      <button class="action-btn" data-click="${tqAct([['tailHistoryCardLog', r.run_id]])}">${i18n.t('btn.tail')}</button>
      <button class="action-btn" data-click="${tqAct([['copyLogTail', r.run_id, `hc-log-pre-${r.run_id}`, TQ_THIS]])}" title="${i18n.t('log.copyTitle')}">${i18n.t('btn.copyLog')}</button>
      <button class="action-btn" data-click="${tqAct([['toggleHistoryCardLog', r.run_id, r.status === 'running']])}" style="margin-left:auto">×</button>
      <div class="log-search-row">
        <input type="text" class="log-search-input" id="log-search-inp-hc-log-pre-${r.run_id}" placeholder="${i18n.t('log.searchPlaceholder')}" data-input="${tqAct([['searchLog', TQ_THIS, r.run_id, `hc-log-pre-${r.run_id}`]])}" style="padding:2px 6px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:11px">
        <button class="action-btn" data-click="${tqAct([['logSearchNav', `hc-log-pre-${r.run_id}`, -1]])}">↑</button>
        <button class="action-btn" data-click="${tqAct([['logSearchNav', `hc-log-pre-${r.run_id}`, 1]])}">↓</button>
        <span id="log-search-count-hc-log-pre-${r.run_id}" style="font-size:10px;color:var(--muted)"></span>
      </div>
    </div>
    <pre class="inline-pre" id="hc-log-pre-${r.run_id}">${logOpen ? _ansiToHtml(S.expandedLogs[logKey].content || 'loading…') : ''}</pre>
  </div>`;
  return `<div class="run-card" data-run-id="${r.run_id}">
    <div class="run-top">
      <span class="run-num">#${r.run_id}</span>
      ${_taskCopyIconBtn(r.project || '', r.task_id)}
      <span class="run-proj">${escHtml(r.project || '')}</span>
      <span class="run-id">${escHtml(r.task_id)}</span>
      <span class="run-worker" style="background:${r.worker_id ? '#4F46E5' : '#6B7280'};color:#fff;font-size:11px;padding:2px 6px;border-radius:4px;font-weight:bold">${escHtml(r.worker_id || 'master')}</span>
      <div class="run-actions">
        ${(_isRestartStatus(r.status) || ['failed','completed'].includes(r.status)) ? `<button class="action-btn" data-click="${tqAct([['retryTaskNew', String(r.project || ''), String(r.task_id), TQ_THIS]])}">${_requeueLabel(r.status)}</button>` : ''}
        <button class="action-btn" id="hc-histbtn-${domIdPart(r.project || '')}-${domIdPart(r.task_id)}" data-click="${tqAct([['toggleHistoryCardHist', String(r.project || ''), String(r.task_id)]])}">${!!S.expandedHistory[`${r.project}/${r.task_id}`] ? i18n.t('btn.hideHistory') : i18n.t('btn.history')}</button>
        ${r.run_id ? `<button class="log-btn" data-click="${tqAct([['showResourceModal', r.run_id]])}" title="${i18n.t('task.metricsTitle')}">📊</button>` : ''}
        ${r.run_id ? `<button class="log-btn" data-click="${tqAct([['compareRun', r.run_id]])}" title="${i18n.t('task.compareTitle')}">⇄</button>` : ''}
        <button class="log-btn" id="hc-logbtn-${r.run_id}" data-click="${tqAct([['toggleHistoryCardLog', r.run_id, r.status === 'running']])}">${logOpen ? i18n.t('btn.hideLog') : i18n.t('btn.log')}</button>
      </div>
    </div>
    <div style="margin-top:4px">${badge(r.status)}${_anomalyBadge(r)}${r.stopped_by ? `<span style="font-size:10px;color:var(--muted);margin-left:4px">↳ ${escHtml(r.stopped_by)}</span>` : ''}${r.error_category ? `<div style="font-size:10px;color:#f59e0b;margin-top:2px">${escHtml(r.error_category)}</div>` : ''}</div>
    ${meta ? `<div style="font-size:10px;color:var(--muted);margin-top:4px">${meta}</div>` : ''}
    ${peak}
    ${logHtml}
    <div id="hc-hist-wrap-${domIdPart(r.project || '')}-${domIdPart(r.task_id)}" style="display:${!!S.expandedHistory[`${r.project}/${r.task_id}`]?'block':'none'};margin-top:8px">
      <div class="inline-log-toolbar">
        <span style="font-weight:600">Run history</span>
        <button class="action-btn" data-click="${tqAct([['toggleHistoryCardHist', String(r.project || ''), String(r.task_id)]])}" style="margin-left:auto">×</button>
      </div>
      <div id="hc-hist-content-${domIdPart(r.project || '')}-${domIdPart(r.task_id)}" style="font-size:11px;padding:4px 0">
        ${!!S.expandedHistory[`${r.project}/${r.task_id}`] ? (S.expandedHistory[`${r.project}/${r.task_id}`] || '<em>loading…</em>') : ''}
      </div>
    </div>
  </div>`;
}

export async function toggleHistoryPageHist(project, taskId, runId) {
  const key = `${project}/${taskId}`;
  const row = document.getElementById(`h-hist-row-${domIdPart(project)}-${domIdPart(taskId)}-${runId}`);
  const btn = document.getElementById(`h-histbtn-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (S.expandedHistory[key]) {
    delete S.expandedHistory[key];
    if (row) row.classList.remove('open');
    if (btn) btn.textContent = i18n.t('btn.history');
    return;
  }
  if (row) row.classList.add('open');
  if (btn) btn.textContent = i18n.t('btn.hideHistory');
  S.expandedHistory[key] = '<em>loading…</em>';
  const content = document.getElementById(`h-hist-content-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (content) content.innerHTML = '<em>loading…</em>';
  S.expandedHistory[key] = await _loadTaskRunHistoryHtml(project, taskId);
  if (content) content.innerHTML = S.expandedHistory[key];
}

export async function toggleHistoryCardHist(project, taskId) {
  const key = `${project}/${taskId}`;
  const wrap = document.getElementById(`hc-hist-wrap-${domIdPart(project)}-${domIdPart(taskId)}`);
  const btn = document.getElementById(`hc-histbtn-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (!wrap) return;
  if (S.expandedHistory[key]) {
    delete S.expandedHistory[key];
    wrap.style.display = 'none';
    if (btn) btn.textContent = i18n.t('btn.history');
    return;
  }
  wrap.style.display = 'block';
  if (btn) btn.textContent = i18n.t('btn.hideHistory');
  const content = document.getElementById(`hc-hist-content-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (content) content.innerHTML = '<em>loading…</em>';
  S.expandedHistory[key] = await _loadTaskRunHistoryHtml(project, taskId);
  if (content) content.innerHTML = S.expandedHistory[key];
}

export function _anomalyBadge(r) {
  if (!r.anomalies || !r.anomalies.length) return '';
  const tip = r.anomalies.map(a => i18n.t('history.anomaly.' + a.metric) + ' ' + a.value + ' (' +
    i18n.t('history.anomaly.typical') + ' ' + a.baseline_p95 + ', ' + a.ratio + '\u00d7)').join('; ');
  return `<span class="anomaly-badge" title="${escHtml(tip)}" style="font-size:10px;color:#f59e0b;margin-left:4px">\u26a0 ${i18n.t('history.anomaly.label')}</span>`;
}

export function renderHistory() {
  let rows = [...S.history];
  if (S.historyStatusFilter !== 'all') rows = rows.filter(r => r.status === S.historyStatusFilter);
  if (S.selectedProject !== 'all') rows = rows.filter(r => r.project === S.selectedProject);
  if (S.globalWorkerFilter) {
    if (S.globalWorkerFilter === 'master') {
      rows = rows.filter(r => !r.worker_id);
    } else {
      rows = rows.filter(r => r.worker_id === S.globalWorkerFilter);
    }
  }

  const {field, dir} = S.historySort;
  rows.sort((a, b) => {
    let av, bv;
    const STATUS_ORDER = { 'running': 1, 'queued': 2, 'interrupted': 3, 'failed': 4, 'unknown': 4, 'completed': 5, 'disabled': 6 };
    if (field === 'run_id') { av = +a.run_id||0; bv = +b.run_id||0; }
    else if (field === 'started_at') { av = a.started_at||''; bv = b.started_at||''; }
    else if (field === 'status') { av = STATUS_ORDER[a.status] || 99; bv = STATUS_ORDER[b.status] || 99; }
    else if (field === 'duration') {
      av = (a.started_at && a.finished_at) ? (new Date(a.finished_at)-new Date(a.started_at)) : 0;
      bv = (b.started_at && b.finished_at) ? (new Date(b.finished_at)-new Date(b.started_at)) : 0;
    }
    return (av < bv ? -1 : av > bv ? 1 : 0) * dir;
  });
  _updateSortTh('h', field, dir);

  if (!rows.length) {
    document.getElementById('history-tbody').innerHTML = `<tr><td colspan="${HISTORY_COLS}" class="empty">${i18n.t('history.noHistory')}</td></tr>`;
    const mobileEmpty = document.getElementById('history-mobile-cards');
    if (mobileEmpty) mobileEmpty.innerHTML = `<div class="empty">${i18n.t('history.noHistory')}</div>`;
    _renderHistoryMore();
    return;
  }
  const mobile = isMobileLayout();
  document.getElementById('history-tbody').innerHTML = mobile ? '' : rows.map(r => {
    const durStr = _historyDuration(r);
    const rowId = `h-log-row-${r.run_id}`;
    // W3.2: panel-scoped key
    const expanded = !!S.expandedLogs[`h:${r.run_id}`];
    const logRowHtml = `<tr class="inline-log-row${expanded?' open':''}" id="${rowId}">
      <td colspan="${HISTORY_COLS}" class="inline-log-wrap">
        <div class="inline-log-toolbar">
          <span>run #${r.run_id} · ${escHtml(r.project||'')}/${escHtml(r.task_id)}</span>
          <button class="action-btn" data-click="${tqAct([['_fetchLog', r.run_id, `h-log-pre-${r.run_id}`]])}">${i18n.t('btn.refresh')}</button>
          <button class="action-btn" data-click="${tqAct([['tailHistoryLog', r.run_id]])}">${i18n.t('btn.tail')}</button>
          <button class="action-btn" data-click="${tqAct([['copyLogTail', r.run_id, `h-log-pre-${r.run_id}`, TQ_THIS]])}" title="${i18n.t('log.copyTitle')}">${i18n.t('btn.copyLog')}</button>
          <input type="text" class="log-search-input" id="log-search-inp-h-log-pre-${r.run_id}" placeholder="${i18n.t('log.searchPlaceholder')}" data-input="${tqAct([['searchLog', TQ_THIS, r.run_id, `h-log-pre-${r.run_id}`]])}" style="width:110px;padding:2px 6px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:11px">
          <button class="action-btn" data-click="${tqAct([['logSearchNav', `h-log-pre-${r.run_id}`, -1]])}" title="${i18n.t('log.prevMatch')}">↑</button>
          <button class="action-btn" data-click="${tqAct([['logSearchNav', `h-log-pre-${r.run_id}`, 1]])}" title="${i18n.t('log.nextMatch')}">↓</button>
          <span id="log-search-count-h-log-pre-${r.run_id}" style="font-size:10px;color:var(--muted);min-width:60px"></span>
          <button class="action-btn" data-click="${tqAct([['toggleHistoryLog', r.run_id, String(rowId), r.status==='running']])}" style="margin-left:auto">×</button>
        </div>
        <pre class="inline-pre" id="h-log-pre-${r.run_id}">${expanded ? _ansiToHtml(S.expandedLogs[`h:${r.run_id}`].content || 'loading…') : 'loading…'}</pre>
      </td>
    </tr>`;
    const histKey = `${r.project}/${r.task_id}`;
    const histOpen = !!S.expandedHistory[histKey];
    const histRowId = `h-hist-row-${domIdPart(r.project || '')}-${domIdPart(r.task_id)}-${r.run_id}`;
    const histPageRowHtml = `<tr class="inline-log-row${histOpen ? ' open' : ''}" id="${histRowId}">
      <td colspan="${HISTORY_COLS}" class="inline-log-wrap">
        <div class="inline-log-toolbar">
          <span style="font-weight:600">Run history · ${escHtml(r.project || '')}/${escHtml(r.task_id)}</span>
          <button class="action-btn" data-click="${tqAct([['toggleHistoryPageHist', String(r.project || ''), String(r.task_id), r.run_id]])}" style="margin-left:auto">×</button>
        </div>
        <div id="h-hist-content-${domIdPart(r.project || '')}-${domIdPart(r.task_id)}" style="padding:6px;font-size:11px">
          ${histOpen ? (S.expandedHistory[histKey] || '<em>loading…</em>') : '<em>loading…</em>'}
        </div>
      </td>
    </tr>`;

    return `<tr>
      <td class="mob-hide mono">#${r.run_id}</td>
      <td class="mono">
        ${_taskCopyIconBtn(r.project || '', r.task_id)}<span class="proj-tag" style="${projStyle(r.project||'')}">${escHtml(r.project||'')}/</span>${escHtml(r.task_id)}
        <span class="worker-tag" style="background:${r.worker_id ? '#4F46E5' : '#6B7280'};color:#fff;font-size:9px;padding:1px 4px;border-radius:3px;font-weight:bold;margin-left:4px">${escHtml(r.worker_id || 'master')}</span>
        ${(r.peak_ram_gib || r.peak_vram_gib) ? `<div style="font-size:10px;color:var(--muted);margin-top:2px;">peak: ${r.peak_ram_gib ? r.peak_ram_gib.toFixed(1) + 'G ram' : ''}${r.peak_vram_gib ? ' / ' + r.peak_vram_gib.toFixed(1) + 'G vram' : ''}</div>` : ''}
      </td>
      <td class="mob-hide">${fmt(r.started_at)}</td>
      <td class="mob-hide">${durStr}</td>
      <td>${badge(r.status)}${r.stopped_by ? `<span style="font-size:10px;color:var(--muted);margin-left:4px" title="stopped by: ${escHtml(r.stopped_by)}">↳ ${escHtml(r.stopped_by)}</span>` : ''}${r.error_category ? `<div style="font-size:10px;color:#f59e0b">${escHtml(r.error_category)}</div>` : ''}</td>
      <td class="mob-hide">${r.exit_code != null ? r.exit_code : '—'}</td>
      <td>${(_isRestartStatus(r.status) || ['failed','completed'].includes(r.status)) ? `<button class="action-btn" data-click="${tqAct([['retryTaskNew', String(r.project||''), String(r.task_id), TQ_THIS]])}">${_requeueLabel(r.status)}</button> ` : ''}<button class="action-btn" id="h-histbtn-${domIdPart(r.project || '')}-${domIdPart(r.task_id)}" data-click="${tqAct([['toggleHistoryPageHist', String(r.project || ''), String(r.task_id), r.run_id]])}">${histOpen ? i18n.t('btn.hideHistory') : i18n.t('btn.history')}</button> ${r.run_id ? `<button class="action-btn" data-click="${tqAct([['showResourceModal', r.run_id]])}" title="${i18n.t('task.metricsTitle')}">📊 metrics</button> <button class="action-btn" data-click="${tqAct([['compareRun', r.run_id]])}" title="${i18n.t('task.compareTitle')}">⇄ ${i18n.t('btn.compare')}</button> <button class="action-btn" data-click="${tqAct([['tqRerunRun', r.run_id]])}" title="${escHtml(i18n.t('history.rerunTitle'))}">↻ ${i18n.t('history.rerun')}</button> ` : ''}<button class="action-btn blue" id="h-logbtn-${r.run_id}" data-click="${tqAct([['toggleHistoryLog', r.run_id, String(rowId), r.status==='running']])}">${expanded ? i18n.t('btn.hideLog') : i18n.t('btn.log')}</button></td>
    </tr>${logRowHtml}${histPageRowHtml}`;
  }).join('');
  const mobileCards = document.getElementById('history-mobile-cards');
  if (mobileCards) mobileCards.innerHTML = mobile ? rows.map(r => _buildHistoryCard(r)).join('') : '';
  // F-110: do not clobber S.prevHistoryStr — refresh.js owns the identity key
  // (full {runs,total,limit,offset} payload). Overwriting with the runs array
  // forced a full History DOM rebuild on every dashboard refresh.

  _renderHistoryMore();
}

onLayoutChange(() => { try { renderHistory(); } catch (_) {} });

/** Search recent run log tails for the text in the history search box (GET /api/log-search). */
export async function searchAllLogs() {
  const inp = document.getElementById('history-search');
  const box = document.getElementById('history-logsearch');
  const q = inp ? inp.value.trim() : '';
  if (!box) return;
  if (!q) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  box.textContent = '…';
  const d = await fetchJson('/api/log-search?q=' + encodeURIComponent(q), 15000);
  if (!d || !Array.isArray(d.matches)) { box.textContent = 'Log search failed'; return; }
  if (!d.matches.length) { box.textContent = 'No log lines match "' + q + '"'; return; }
  const rows = d.matches.map(m =>
    '<div class="logsearch-row"><a href="#" data-click-prevent data-click="' + tqAct([['openLogSearchRun', m.run_id]]) + '">#' + escHtml(String(m.run_id)) +
    '</a> <b>' + escHtml(m.project) + '/' + escHtml(m.task_id) + '</b> <code>' + escHtml(m.line) + '</code></div>').join('');
  box.innerHTML = '<div class="logsearch-head">' + d.matches.length + (d.truncated ? '+' : '') + ' match(es)</div>' + rows;
}

export function openLogSearchRun(runId) {
  const inp = document.getElementById('history-search');
  if (inp) inp.value = '#' + runId;
  _syncHistorySearchClear();
  S.historyLimit = S.historyPageSize;
  refresh();
  return false;
}
