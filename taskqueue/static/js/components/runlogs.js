// Per-run log plumbing shared by queue, history and dashboard: live SSE maps (S.logSseMap / S.cardLogSseMap),
// per-task run-history toggles, the one-shot log fetch with ANSI colouring, copy helpers and log-issue jumps.
// Uses app.js and other-module globals at call time (i18n, tqAct, showAnchorToast, _applyLogSearch, openLogStream, ...).
import { S } from '../core/state.js';
import { latest } from '../core/api.js';
import { escHtml, taskKey, domIdPart, LOG_CAP_CHARS } from '../util/format.js';
import { trackLogSse, ansiToHtml as _ansiToHtml } from './logview.js';
export { _ansiToHtml };

// E2/E3: SSE EventSources keyed by runId live in S.logSseMap / S.cardLogSseMap (core/state.js).
/** Register a log EventSource (see js/components/logview.js trackLogSse). */
export function _trackLogSse(map, runId, es) { trackLogSse([S.logSseMap, S.cardLogSseMap], map, runId, es); }
if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('pagehide', () => {
  [S.logSseMap, S.cardLogSseMap].forEach(m => Object.keys(m).forEach(k => {
    try { m[k].close(); } catch (e) {}
    delete m[k];
  }));
});


export async function toggleTaskHistory(project, taskId, rowId) {
  const key = `${project}/${taskId}`;
  const row = document.getElementById(rowId);
  const btn = document.getElementById(`q-histbtn-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (!row) return;
  if (S.expandedHistory[key]) {
    delete S.expandedHistory[key];
    delete S._histGen?.[key];
    row.classList.remove('open');
    if (btn) btn.textContent = i18n.t('btn.history');
    return;
  }
  S._histGen = S._histGen || {};
  const gen = (S._histGen[key] = (S._histGen[key] || 0) + 1);
  S.expandedHistory[key] = '<em>loading…</em>';
  row.classList.add('open');
  if (btn) btn.textContent = i18n.t('btn.hideHistory');
  const html = await _loadTaskRunHistoryHtml(project, taskId);
  // F-133: ignore late response after collapse / newer open.
  if (S._histGen[key] !== gen || !S.expandedHistory[key]) return;
  S.expandedHistory[key] = html;
  const content = document.getElementById(`q-hist-content-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (content) content.innerHTML = html;
}

export async function _loadTaskRunHistoryHtml(project, taskId) {
  try {
    const [runsData, statsData] = await Promise.all([
      fetch(`/api/tasks/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}/runs`).then(r => r.json()),
      fetch(`/api/task-stats?project=${encodeURIComponent(project)}&task_id=${encodeURIComponent(taskId)}`).then(r => r.json()).catch(() => ({ tasks: [] })),
    ]);
    const runs = runsData.runs || [];
    const stats = ((statsData.tasks || []).find(s => s.task_id === taskId && s.project === project))
      || (statsData.tasks || [])[0]
      || null;
    return _renderTaskRunHistoryHtml(project, taskId, runs, stats);
  } catch (e) {
    return `<em style="color:#EF4444">Failed to load history: ${escHtml(String(e))}</em>`;
  }
}

export function _renderTaskRunHistoryHtml(project, taskId, runs, stats) {
  const statsStrip = _taskStatsStripHtml(stats);
  if (!runs.length) {
    return `${statsStrip}<em style="color:var(--muted)">No runs recorded yet.</em>`;
  }
  const shown = runs.slice(0, 20);
  const more = runs.length > 20
    ? `<div style="font-size:10px;color:var(--muted);padding:4px 8px">Showing latest 20 of ${runs.length} runs.</div>`
    : '';
  const statusColors = {running:'#3B82F6',completed:'#10B981',failed:'#EF4444',unknown:'#A855F7',interrupted:'#F59E0B',stopped:'#6B7280',queued:'#8B5CF6'};
  const rows = shown.map(r => {
    const sc = statusColors[r.status] || '#6B7280';
    const startedMs = r.started_at ? new Date(r.started_at).getTime() : null;
    const finishedMs = r.finished_at ? new Date(r.finished_at).getTime() : null;
    const started = startedMs ? new Date(startedMs).toLocaleString() : '—';
    const elapsed = (startedMs && finishedMs) ? Math.round((finishedMs - startedMs) / 1000) : null;
    const durStr = elapsed != null ? dur(elapsed) : '—';
    const ram = r.peak_ram_gib ? r.peak_ram_gib.toFixed(1) + ' GiB' : '—';
    const vram = r.peak_vram_gib ? r.peak_vram_gib.toFixed(1) + ' GiB' : '—';
    const cpu = r.peak_cpu_pct != null ? fmtCpu(r.peak_cpu_pct) : '—';
    const worker = r.worker_id ? `<span style="font-size:9px;background:#4F46E5;color:#fff;padding:1px 4px;border-radius:3px">@${escHtml(r.worker_id)}</span>` : '—';
    const exitCode = r.exit_code != null ? r.exit_code : '—';
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:3px 8px;color:var(--muted)">#${r.run_id}</td>
      <td style="padding:3px 8px"><span style="color:${sc};font-weight:600">${escHtml(r.status)}</span></td>
      <td style="padding:3px 8px">${started}</td>
      <td style="padding:3px 8px">${durStr}</td>
      <td style="padding:3px 8px">${ram}</td>
      <td style="padding:3px 8px">${vram}</td>
      <td style="padding:3px 8px">${cpu}</td>
      <td style="padding:3px 8px">${worker}</td>
      <td style="padding:3px 8px">${exitCode}</td>
    </tr>`;
  }).join('');
  return `${statsStrip}<table style="width:100%;border-collapse:collapse;font-size:11px">
    <thead><tr style="color:var(--muted);text-align:left">
      <th style="padding:3px 8px">Run</th><th style="padding:3px 8px">Status</th>
      <th style="padding:3px 8px">Started</th><th style="padding:3px 8px">Duration</th>
      <th style="padding:3px 8px">Peak RAM</th><th style="padding:3px 8px">Peak VRAM</th>
      <th style="padding:3px 8px">Peak CPU</th><th style="padding:3px 8px">Worker</th>
      <th style="padding:3px 8px">Exit</th>
    </tr></thead><tbody>${rows}</tbody></table>${more}`;
}

export function _taskStatsStripHtml(stats) {
  if (!stats) {
    return `<div class="tasks-stats-strip"><span>${escHtml(i18n.t('tasks.noStats') || 'No duration stats yet')}</span></div>`;
  }
  const avg = stats.mean_sec != null ? dur(stats.mean_sec) : '—';
  const p50 = stats.p50_sec != null ? dur(stats.p50_sec) : '—';
  const p95 = stats.p95_sec != null ? dur(stats.p95_sec) : '—';
  const success = Math.round((stats.success_rate || 0) * 100) + '%';
  return `<div class="tasks-stats-strip">
    <span>${escHtml(i18n.t('tasks.colMean') || 'Average')}: <strong>${escHtml(avg)}</strong></span>
    <span>P50: <strong>${escHtml(p50)}</strong></span>
    <span>P95: <strong>${escHtml(p95)}</strong></span>
    <span>${escHtml(i18n.t('tasks.colCount') || 'Runs')}: <strong>${escHtml(String(stats.count || 0))}</strong></span>
    <span>${escHtml(i18n.t('tasks.colSuccess') || 'Success')}: <strong>${escHtml(success)}</strong></span>
  </div>`;
}

export async function toggleCardLog(runId, project, taskId, isRunning) {
  const key = `qc:${runId}`;
  const wrap = document.getElementById(`qc-log-wrap-${runId}`);
  if (!wrap) return;
  const btn = document.getElementById(`qc-logbtn-${runId}`);
  if (S.expandedLogs[key]) {
    delete S.expandedLogs[key];
    wrap.style.display = 'none';
    if (btn) btn.textContent = i18n.t('btn.log');
    if (S.cardLogSseMap[runId]) { S.cardLogSseMap[runId].close(); delete S.cardLogSseMap[runId]; }
    return;
  }
  S.expandedLogs[key] = { content: '', isRunning };
  wrap.style.display = 'block';
  if (btn) btn.textContent = i18n.t('btn.hideLog');
  wrap.scrollIntoView({ block: 'nearest' });
  if (isRunning && typeof EventSource !== 'undefined') {
    openLogStream({ runId, maps: [S.logSseMap, S.cardLogSseMap], map: S.cardLogSseMap, preId: `qc-log-pre-${runId}`,
      followId: `qc-log-follow-${runId}`, expanded: S.expandedLogs, stateKey: key });
  } else {
    await _fetchLog(runId, `qc-log-pre-${runId}`);
  }
}

export async function toggleCardHistory(project, taskId) {
  const key = `${project}/${taskId}`;
  const wrap = document.getElementById(`qc-hist-wrap-${domIdPart(project)}-${domIdPart(taskId)}`);
  const btn = document.getElementById(`qc-histbtn-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (!wrap) return;
  if (S.expandedHistory[key]) {
    delete S.expandedHistory[key];
    delete S._histGen?.[key];
    wrap.style.display = 'none';
    if (btn) btn.textContent = i18n.t('btn.history');
    return;
  }
  S._histGen = S._histGen || {};
  const gen = (S._histGen[key] = (S._histGen[key] || 0) + 1);
  wrap.style.display = 'block';
  if (btn) btn.textContent = i18n.t('btn.hideHistory');
  const content = document.getElementById(`qc-hist-content-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (content) content.innerHTML = '<em>loading…</em>';
  const html = await _loadTaskRunHistoryHtml(project, taskId);
  if (S._histGen[key] !== gen || !wrap || wrap.style.display === 'none') return;
  S.expandedHistory[key] = html;
  if (content) content.innerHTML = html;
}

// ---- Shared log fetch ----

// W3.2: _fetchLog now writes only to the requesting panel's preId.
// Panel key helpers: 'd:', 'q:', 'h:' prefixed by panel.
export function _panelKey(preId, runId) {
  if (preId && preId.startsWith('d-')) return `d:${runId}`;
  if (preId && preId.startsWith('qc-')) return `qc:${runId}`;
  if (preId && preId.startsWith('q-')) return `q:${runId}`;
  if (preId && preId.startsWith('hc-')) return `h:${runId}`;
  if (preId && preId.startsWith('h-')) return `h:${runId}`;
  return `d:${runId}`;
}

// Max characters kept in a live-SSE log <pre> before old content is trimmed.
// insertAdjacentHTML avoids O(n²) re-parse, but the DOM node still grows
// without a cap. Same cap as stored log content (LOG_CAP_CHARS) so the pane and the copy agree.
const _SSE_DOM_CAP = LOG_CAP_CHARS;

// Append rendered HTML to a log <pre> using insertAdjacentHTML (O(new content),
// not O(total content) like innerHTML +=). Trims from the top when the text
// content exceeds _SSE_DOM_CAP, adding a banner so the user knows.
export function _sseAppend(pre, html) {
  pre._logSig = null;
  pre._logNext = null; // DOM no longer mirrors _logRaw: next poll refetches the tail
  pre.insertAdjacentHTML('beforeend', html);
  // Check size every 20 appends to amortise the textContent read cost.
  pre._sseCount = (pre._sseCount || 0) + 1;
  if (pre._sseCount % 20 !== 0) return;
  if (pre.textContent.length <= _SSE_DOM_CAP) return;
  // Keep the tail: slice text to cap, align to next newline.
  const text = pre.textContent;
  const cut = text.length - _SSE_DOM_CAP;
  const nl = text.indexOf('\n', cut);
  const kept = nl >= 0 ? text.slice(nl + 1) : text.slice(cut);
  // Rebuild innerHTML from kept text (re-render ANSI for the kept portion).
  const banner = `<span style="color:var(--muted);font-style:italic">` +
    `--- ${Math.round((text.length - kept.length) / 1024)} KB omitted — showing tail ---\n</span>`;
  pre.innerHTML = banner + _ansiToHtml(kept);
}

// W3.4: ANSI escape code to CSS colour mapping (common SGR codes only).

export async function _fetchLog(runId, preId) {
  // Only write to the single panel that requested this fetch.
  const targetId = preId || `d-log-pre-${runId}`;
  const pre = document.getElementById(targetId);
  if (!pre) return;

  const isAtBottom = pre.scrollHeight - pre.clientHeight <= pre.scrollTop + 30;
  const scrollTop = pre.scrollTop;

  const live = latest('log:' + targetId);
  try {
    // Server returns a bounded byte-tail (default 256KiB, max 8MiB) — never the full file.
    // After the first fetch only the bytes past the last offset are requested (X-Log-Next).
    const inc = pre._logNext != null && typeof pre._logRaw === 'string';
    const r = await fetch(`/api/logs/${runId}` + (inc ? `?from=${pre._logNext}` : ''));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    let text = await r.text();
    if (!live()) return;
    const next = r.headers.get('X-Log-Next');
    if (inc && r.headers.get('X-Log-From') != null) {
      if (!text) return; // nothing new: keep the DOM as is
      pre._logNext = next != null ? parseInt(next, 10) : null;
      pre._logRaw = (pre._logRaw + text).slice(-LOG_CAP_CHARS);
      text = pre._logRaw;
    } else {
      pre._logNext = next != null ? parseInt(next, 10) : null;
      pre._logRaw = text;
    }
    const key = _panelKey(targetId, runId);
    if (S.expandedLogs[key]) S.expandedLogs[key].content = text;

    // Show file-size banner when the log was truncated server-side.
    const fileSize = parseInt(r.headers.get('X-Log-Size') || '0', 10);
    const truncated = r.headers.get('X-Log-Truncated') === '1';
    let banner = inc && r.headers.get('X-Log-From') != null ? (pre._logBanner || '') : '';
    if (!inc && truncated && fileSize > 0) {
      const mb = (fileSize / (1024 * 1024)).toFixed(1);
      banner = `<span style="color:var(--muted);font-style:italic">` +
        `--- log is ${mb} MB — showing last 256 KB ---\n</span>`;
    }

    pre._logBanner = banner;
    // Unchanged tail: keep the existing DOM (no re-parse, scroll or search highlights reset).
    const sig = banner + '\u0000' + text;
    if (pre._logSig === sig) return;
    pre._logSig = sig;
    // W3.4: render ANSI codes as coloured HTML; log text already HTML-escaped inside _ansiToHtml.
    pre.innerHTML = banner + _ansiToHtml(text);
    // Re-apply any active search after content is updated
    const searchState = S.logSearch[targetId];
    if (searchState && searchState.query) {
      _applyLogSearch(targetId, searchState.query, searchState.current || 0);
    }
    if (isAtBottom) {
      pre.scrollTop = pre.scrollHeight;
    } else {
      pre.scrollTop = scrollTop;
    }
  } catch(e) {
    if (live()) { pre._logSig = null; pre.textContent = `error loading log: ${e}`; }
  }
}

const LOG_COPY_MAX_LINES = 100;

/** Copy project/task_id to the clipboard (e.g. pimc/ml_train_play_L). */
export async function copyTaskName(project, taskId, btn) {
  const text = taskKey(project, taskId);
  try {
    await navigator.clipboard.writeText(text);
    showAnchorToast(btn, i18n.t('toast.copiedName', { name: text }), 'ok', 2000);
    if (btn) {
      btn.classList.add('task-copy-btn-ok');
      const title = i18n.t('task.copyNameDone');
      btn.title = title;
      btn.setAttribute('aria-label', title);
      setTimeout(() => {
        btn.classList.remove('task-copy-btn-ok');
        const restore = i18n.t('task.copyNameTitle');
        btn.title = restore;
        btn.setAttribute('aria-label', restore);
      }, 1200);
    }
  } catch (e) {
    openAlert('Copy failed: ' + e, i18n.t('modal.copyFailedTitle'));
  }
}

/** Copy last N lines of a log panel to the clipboard (plain text, no ANSI). */
export async function copyLogTail(runId, preId, btn) {
  let text = '';
  const key = _panelKey(preId, runId);
  if (S.expandedLogs[key] && typeof S.expandedLogs[key].content === 'string' && S.expandedLogs[key].content) {
    text = S.expandedLogs[key].content;
  } else {
    const pre = document.getElementById(preId);
    text = pre ? (pre.innerText || pre.textContent || '') : '';
    // Drop UI truncation banner if present in DOM text.
    text = text.replace(/^--- log is .*? ---\n/, '');
  }
  const tail = logTailText(text, LOG_COPY_MAX_LINES);
  if (!tail.trim() || tail === 'loading…' || tail === i18n.t('log.loading')) {
    openAlert(i18n.t('log.copyEmpty'));
    return;
  }
  try {
    await navigator.clipboard.writeText(tail);
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = i18n.t('btn.copied');
      setTimeout(() => { btn.textContent = orig; }, 1500);
    }
  } catch (e) {
    openAlert('Copy failed: ' + e, i18n.t('modal.copyFailedTitle'));
  }
}

// W9.8: search within an already-loaded log pre
const _ISSUE_SEARCH = { warn: 'WARN', err: 'ERROR', other: 'LOW SCORE' };

export function _resolveLogPanel(runId, opts) {
  opts = opts || {};
  if (document.getElementById(`d-log-wrap-${runId}`)) {
    return { panel: 'd', project: opts.project || '', taskId: opts.taskId || '' };
  }
  if (document.getElementById(`qc-log-wrap-${runId}`)) {
    return {
      panel: 'qc',
      project: opts.project || '',
      taskId: opts.taskId || '',
      isRunning: !!opts.isRunning,
    };
  }
  if (document.getElementById(`q-log-row-${runId}`) || document.getElementById(`q-logbtn-${runId}`)) {
    return {
      panel: 'q',
      rowId: opts.rowId || `q-log-row-${runId}`,
      isRunning: !!opts.isRunning,
    };
  }
  return null;
}

export async function _ensureLogOpen(runId, panel, opts) {
  opts = opts || {};
  const key = `${panel}:${runId}`;
  const preId = `${panel}-log-pre-${runId}`;
  if (S.expandedLogs[key]) {
    if (!S.expandedLogs[key].content) await _fetchLog(runId, preId);
    return;
  }
  if (panel === 'd') {
    const wrap = document.getElementById(`d-log-wrap-${runId}`);
    const btn = document.getElementById(`d-logbtn-${runId}`);
    if (!wrap) return;
    S.expandedLogs[key] = { content: '', isRunning: true };
    wrap.classList.add('open');
    if (btn) btn.textContent = i18n.t('btn.hideLog');
    await _fetchLog(runId, preId);
    return;
  }
  if (panel === 'q') {
    const rowId = opts.rowId || `q-log-row-${runId}`;
    await toggleQueueLog(runId, rowId, !!opts.isRunning);
    return;
  }
  if (panel === 'qc') {
    await toggleCardLog(runId, opts.project || '', opts.taskId || '', !!opts.isRunning);
  }
}

export async function openLogIssue(runId, panel, kind, project, taskId, rowId, isRunning) {
  if (!runId) return;
  let opts = {
    project: project || '',
    taskId: taskId || '',
    rowId: rowId || `q-log-row-${runId}`,
    isRunning: !!isRunning,
  };
  if (panel === 'auto') {
    const resolved = _resolveLogPanel(runId, opts);
    if (!resolved) return;
    panel = resolved.panel;
    opts = Object.assign({}, opts, resolved);
  }
  const query = _ISSUE_SEARCH[kind] || 'WARN';
  const preId = `${panel}-log-pre-${runId}`;
  const qLower = query.toLowerCase();
  // Prefill before fetch so _fetchLog re-applies search on paint.
  S.logSearch[preId] = { query: qLower, current: 0 };
  const inpEarly = document.getElementById('log-search-inp-' + preId);
  if (inpEarly) inpEarly.value = query;

  await _ensureLogOpen(runId, panel, opts);

  const inp = document.getElementById('log-search-inp-' + preId);
  if (inp) inp.value = query;
  S.logSearch[preId] = { query: qLower, current: 0 };
  _applyLogSearch(preId, qLower, 0);

  const modal = document.getElementById('resource-modal');
  if (modal && modal.style.display !== 'none') closeResourceModal();
}
