// Dashboard tab: resource gauges, running-task cards, per-task history/log panels and the workers table.
// Uses app.js globals at call time (i18n, badge, tqAct, _computeGaugeModel, _patchDashboardLive, _fetchLog, etc.).
import { S } from '../core/state.js';
import { projStyle } from '../util/projcolor.js';
import { escHtml, jsq, taskKey, domIdPart, fmt, dur, progressHealthHtml } from '../util/format.js';

export async function renderDashboard() {
  const g = _computeGaugeModel();
  const { sys, cfg, cpuLimit, gpuLimit, ramLimit, ramUsed, ramTotal, cpuUsed, gpuUsed, ramPct, ramColor } = g;

  document.getElementById('gauges').innerHTML = `
    <div class="gauge" data-gauge="cpu">
      <div class="gauge-label">${i18n.t('gauge.cpuCores')}</div>
      <div class="gauge-val" data-g="val">${cpuUsed.toFixed(0)} / ${cpuLimit.toFixed(0)}</div>
      <div class="gauge-sub" data-g="sub">${i18n.t('gauge.totalCores', {n: sys.cpu_total_cores||0})}</div>
      <div class="gauge-bar"><div class="gauge-bar-fill bar-cpu" data-g="fill" style="width:${Math.min(100,cpuUsed/Math.max(cpuLimit,1)*100)}%"></div></div>
      <svg class="spark" data-spark="cpu" viewBox="0 0 100 48" preserveAspectRatio="none" aria-hidden="true"></svg>
      <div class="spark-range" data-spark-cap="cpu"></div>
    </div>
    <div class="gauge" data-gauge="gpu">
      <div class="gauge-label">${i18n.t('gauge.gpuVram')}</div>
      <div class="gauge-val" data-g="val">${gpuUsed.toFixed(1)} / ${gpuLimit.toFixed(1)} GiB</div>
      <div class="gauge-sub" data-g="sub">${sys.gpu_name||i18n.t('gauge.noGpu')}</div>
      <div class="gauge-bar"><div class="gauge-bar-fill bar-gpu" data-g="fill" style="width:${gpuLimit?Math.min(100,gpuUsed/gpuLimit*100):0}%"></div></div>
      <svg class="spark" data-spark="gpu" viewBox="0 0 100 48" preserveAspectRatio="none" aria-hidden="true"></svg>
      <div class="spark-range" data-spark-cap="gpu"></div>
    </div>
    <div class="gauge" data-gauge="ram">
      <div class="gauge-label">${i18n.t('gauge.ramUsed')}</div>
      <div class="gauge-val" data-g="val" style="color:${ramColor}">${ramUsed.toFixed(1)} / ${ramLimit.toFixed(0)} GiB</div>
      <div class="gauge-sub" data-g="sub">${ramTotal.toFixed(0)} GiB total · queue limit ${(cfg.ram_limit_pct||50)}%</div>
      <div class="gauge-bar"><div class="gauge-bar-fill bar-ram" data-g="fill" style="width:${ramPct.toFixed(1)}%"></div></div>
      <svg class="spark" data-spark="ram" viewBox="0 0 100 48" preserveAspectRatio="none" aria-hidden="true"></svg>
      <div class="spark-range" data-spark-cap="ram"></div>
    </div>
    <div class="gauge" data-gauge="queued">
      <div class="gauge-label">Queued</div>
      <div class="gauge-val" data-g="val">${S.status.queued_count||0}</div>
      <div class="gauge-sub" data-g="sub">${(S.status.running||[]).length} running</div>
      <div class="gauge-bar"><div class="gauge-bar-fill bar-cpu" style="width:0%"></div></div>
      <svg class="spark" data-spark="queued running" viewBox="0 0 100 48" preserveAspectRatio="none" aria-hidden="true"></svg>
      <div class="spark-range" data-spark-cap="queued running"></div>
      <div class="spark-legend"><span class="spark-key spark-key-queued">${i18n.t('filter.queued')}</span><span class="spark-key spark-key-running">${i18n.t('filter.running')}</span></div>
    </div>
    <div class="gauge" data-gauge="daemon">
      <div class="gauge-label">Daemon</div>
      <div class="gauge-val" data-g="val">${S.status.daemon_rss_mb != null ? S.status.daemon_rss_mb.toFixed(0)+' MB' : '—'}</div>
      <div class="gauge-sub" data-g="sub">${S.status.daemon_cpu_pct != null ? 'CPU '+S.status.daemon_cpu_pct+'%' : ''} RSS</div>
      <div class="gauge-bar"><div class="gauge-bar-fill bar-ram" data-g="fill" style="width:${Math.min(100,(S.status.daemon_rss_mb||0)/5).toFixed(1)}%"></div></div>
      <svg class="spark" data-spark="daemon" viewBox="0 0 100 48" preserveAspectRatio="none" aria-hidden="true"></svg>
      <div class="spark-range" data-spark-cap="daemon"></div>
    </div>`;

  let running = S.status.running || [];
  if (S.globalWorkerFilter) {
    if (S.globalWorkerFilter === 'master') {
      running = running.filter(t => !t.worker_id);
    } else {
      running = running.filter(t => t.worker_id === S.globalWorkerFilter);
    }
  }
  if (!running.length) {
    document.getElementById('running-list').innerHTML = `<div class="empty">${i18n.t('dashboard.noTasksRunning')}</div>`;
    S.prevRunningStr = _runningIdentity(S.status.running, S.cfg);
    S.prevGaugeStructStr = _gaugeStructureKey(S.status, S.cfg);
    tqRecordSparks();
    return;
  }
  const frozenThresh = parseFloat(S.cfg.frozen_threshold_sec || 600);
  await _ensureDashStats(running);
  document.getElementById('running-list').innerHTML = running.map(t => {
    const pct = t.progress_pct != null ? t.progress_pct : 0;
    const pctKnown = t.progress_pct != null;
    const elapsed = dur(t.elapsed_sec);
    const idleSec = t.idle_sec || 0;
    const isFrozen = idleSec > frozenThresh;
    const expectedSec = _dashExpectedSec(t.project, t.task_id);
    const overlong = expectedSec != null && t.elapsed_sec != null && t.elapsed_sec > expectedSec;
    const avgHint = expectedSec != null ? ` / ~avg ${dur(expectedSec)}` : '';
    let eta = '';
    const rem = _etaRemainingSec(t, t.elapsed_sec, pctKnown ? pct : 0);
    if (rem != null) eta = ` · ETA ~${dur(rem)}`;
    // Keep a line even when idle ≤5s so cards don't jump as idle crosses the threshold.
    const lastTs = idleSec > 5 ? ` · last output ${dur(idleSec)} ago` : '\u00a0';
    // W3.2: panel-scoped key for dashboard panel
    const logExpanded = t.run_id && !!S.expandedLogs[`d:${t.run_id}`];
    const logContent = logExpanded ? _ansiToHtml(S.expandedLogs[`d:${t.run_id}`].content || '') : '';
    const histKey = `${t.project}/${t.task_id}`;
    const histOpen = !!S.expandedHistory[histKey];
    return `<div class="run-card${isFrozen?' frozen':''}" data-run-id="${t.run_id}">
      <div class="run-top">
        ${t.run_id ? `<span class="run-num" title="run_id">#${t.run_id}</span>` : ''}
        ${_taskCopyIconBtn(t.project || '', t.task_id)}
        <span class="run-proj run-proj-link" style="border-left:3px solid var(--pc);padding-left:5px;${projStyle(t.project||'')}" title="Open project in Queue" data-click="${tqAct([['gotoQueueProject', String(t.project||'')]])}" data-click-stop>${escHtml(t.project||'')}</span>
        <span class="run-id">${escHtml(t.task_id)}</span>
        ${_gpuBadgeHtml(t)}
        <span class="run-worker" style="background:${t.worker_id ? '#4F46E5' : '#6B7280'};color:#fff;font-size:11px;padding:2px 6px;border-radius:4px;font-weight:bold;margin-left:8px">${escHtml(t.worker_id || 'master')}</span>
        <span class="run-elapsed${overlong ? ' overlong' : ''}" data-elapsed-run-id="${t.run_id}">${elapsed}${avgHint}</span>
        ${pctKnown ? `<span class="run-eta${overlong ? ' overlong' : ''}" data-eta-run-id="${t.run_id}">${pct.toFixed(0)}%${eta}</span>` : (avgHint ? `<span class="run-eta${overlong ? ' overlong' : ''}">${avgHint.trim()}</span>` : '')}
        ${isFrozen ? `<span class="frozen-badge">FROZEN ${dur(idleSec)} no output</span>` : ''}
        <div class="run-actions">
          ${t.run_id ? `<button class="log-btn" id="d-logbtn-${t.run_id}" data-click="${tqAct([['toggleDashLog', t.run_id, String(t.project), String(t.task_id)]])}">${logExpanded ? i18n.t('btn.hideLog') : i18n.t('btn.log')}</button>` : ''}
          <button class="action-btn" id="d-histbtn-${domIdPart(t.project)}-${domIdPart(t.task_id)}" data-click="${tqAct([['toggleDashHist', String(t.project), String(t.task_id)]])}">${histOpen ? i18n.t('btn.hideHistory') : i18n.t('btn.history')}</button>
          ${t.run_id ? `<button class="log-btn" data-click="${tqAct([['showResourceModal', t.run_id]])}" title="${i18n.t('task.metricsTitle')}">📊 metrics</button>` : ''}
          <button class="stop-btn" data-click="${tqAct([['stopTask', TQ_THIS, String(t.project), String(t.task_id)]])}">${i18n.t('btn.stop')}</button>
        </div>
      </div>
      <div class="pbar-wrap"><div class="pbar" data-pbar-run-id="${t.run_id}" style="width:${pct}%"></div></div>
      ${progressHealthHtml(t.progress_health)}
      <div class="run-log" data-log-run-id="${t.run_id}">${t.last_log_line != null && t.last_log_line !== '' ? escHtml(t.last_log_line) : '—'}</div>
      <div class="run-last-ts" data-last-ts-run-id="${t.run_id}">${lastTs}</div>
      <div id="d-hist-wrap-${domIdPart(t.project)}-${domIdPart(t.task_id)}" style="display:${histOpen ? 'block' : 'none'}"><div id="d-hist-content-${domIdPart(t.project)}-${domIdPart(t.task_id)}">${histOpen ? (S.expandedHistory[histKey] || '<em>loading…</em>') : ''}</div></div>
      ${t.run_id ? `<div id="dash-metrics-${t.run_id}" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;min-height:1em">${_usageHtml(t, { panel: 'd' })}</div>` : ''}
      ${t.run_id ? `<div class="dash-log-wrap${logExpanded?' open':''}" id="d-log-wrap-${t.run_id}">
        <div class="inline-log-toolbar">
          <span>run #${t.run_id} · ${escHtml(t.project||'')}/${escHtml(t.task_id)}</span>
          <button class="action-btn" data-click="${tqAct([['refreshDashLog', t.run_id]])}">${i18n.t('btn.refresh')}</button>
          <button class="action-btn" data-click="${tqAct([['tailDashLog', t.run_id]])}">${i18n.t('btn.tail')}</button>
          <button class="action-btn" data-click="${tqAct([['copyLogTail', t.run_id, `d-log-pre-${t.run_id}`, TQ_THIS]])}" title="${i18n.t('log.copyTitle')}">${i18n.t('btn.copyLog')}</button>
          <input type="text" class="log-search-input" id="log-search-inp-d-log-pre-${t.run_id}" placeholder="${i18n.t('log.searchPlaceholder')}" data-input="${tqAct([['searchLog', TQ_THIS, t.run_id, `d-log-pre-${t.run_id}`]])}" style="width:110px;padding:2px 6px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:11px">
          <button class="action-btn" data-click="${tqAct([['logSearchNav', `d-log-pre-${t.run_id}`, -1]])}" title="${i18n.t('log.prevMatch')}">↑</button>
          <button class="action-btn" data-click="${tqAct([['logSearchNav', `d-log-pre-${t.run_id}`, 1]])}" title="${i18n.t('log.nextMatch')}">↓</button>
          <span id="log-search-count-d-log-pre-${t.run_id}" style="font-size:10px;color:var(--muted);min-width:60px"></span>
          <button class="action-btn" data-click="${tqAct([['toggleDashLog', t.run_id, String(t.project||''), String(t.task_id)]])}" style="margin-left:auto">×</button>
        </div>
        <pre class="inline-pre" id="d-log-pre-${t.run_id}">${logContent || (logExpanded ? 'loading…' : '')}</pre>
      </div>` : ''}
    </div>`;
  }).join('');

  // Fetch log content for any expanded dashboard log that needs it
  running.forEach(t => {
    if (t.run_id && S.expandedLogs[`d:${t.run_id}`] && !S.expandedLogs[`d:${t.run_id}`].content) {
      _fetchLog(t.run_id, `d-log-pre-${t.run_id}`);
    }
  });

  S.prevRunningStr = _runningIdentity(S.status.running, S.cfg);
  S.prevGaugeStructStr = _gaugeStructureKey(S.status, S.cfg);
  tqRecordSparks();
}

export function _dashExpectedSec(project, taskId) {
  const s = S.dashStatsByKey[`${project}/${taskId}`];
  if (!s) return null;
  if (s.mean_sec != null) return Number(s.mean_sec);
  if (s.p50_sec != null) return Number(s.p50_sec);
  return null;
}

export async function _ensureDashStats(running) {
  const need = [];
  const seen = new Set();
  (running || []).forEach(t => {
    const k = `${t.project}/${t.task_id}`;
    if (S.dashStatsByKey[k] !== undefined) return;
    if (seen.has(t.project)) return;
    seen.add(t.project);
    need.push(t.project);
  });
  await Promise.all(need.map(async proj => {
    try {
      const data = await _fetchJson('/api/task-stats?project=' + encodeURIComponent(proj), 8000, true);
      ((data && data.tasks) || []).forEach(row => {
        S.dashStatsByKey[`${row.project}/${row.task_id}`] = row;
      });
      (running || []).filter(t => t.project === proj).forEach(t => {
        const k = `${t.project}/${t.task_id}`;
        if (S.dashStatsByKey[k] === undefined) S.dashStatsByKey[k] = null;
      });
    } catch (e) {
      (running || []).filter(t => t.project === proj).forEach(t => {
        const k = `${t.project}/${t.task_id}`;
        if (S.dashStatsByKey[k] === undefined) S.dashStatsByKey[k] = null;
      });
    }
  }));
}

export async function toggleDashHist(project, taskId) {
  const key = `${project}/${taskId}`;
  const wrap = document.getElementById(`d-hist-wrap-${domIdPart(project)}-${domIdPart(taskId)}`);
  const btn = document.getElementById(`d-histbtn-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (!wrap) return;
  if (S.expandedHistory[key]) {
    delete S.expandedHistory[key];
    wrap.style.display = 'none';
    if (btn) btn.textContent = i18n.t('btn.history');
    return;
  }
  wrap.style.display = 'block';
  if (btn) btn.textContent = i18n.t('btn.hideHistory');
  const content = document.getElementById(`d-hist-content-${domIdPart(project)}-${domIdPart(taskId)}`);
  if (content) content.innerHTML = '<em>loading…</em>';
  S.expandedHistory[key] = await _loadTaskRunHistoryHtml(project, taskId);
  if (content) content.innerHTML = S.expandedHistory[key];
}

export function renderWorkers() {
  const tbody = document.getElementById('workers-tbody');
  if (!S.workers || S.workers.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:20px;color:var(--text2)">${i18n.t('workers.noWorkers')}</td></tr>`;
    return;
  }
  const _jl = v => { if (Array.isArray(v)) return v; try { const x = JSON.parse(v || '[]'); return Array.isArray(x) ? x : []; } catch (_) { return []; } };
  const _n = v => { const x = Number(v); return Number.isFinite(x) ? x : 0; };
  tbody.innerHTML = S.workers.map(w => { try {
    const projects = _jl(w.projects);
    const isOnline = w.status === 'online';
    const statusColor = isOnline ? '#10B981' : '#EF4444';
    const lastSeenStr = w.last_seen ? escHtml(new Date(w.last_seen).toLocaleString()) : 'never';
    
    const runsOnWorker = (S.status.running || []).filter(r => r.worker_id === w.worker_id);
    const coresUsed = runsOnWorker.reduce((sum, r) => sum + (r.resources?.cpu_cores || 1), 0);
    const ramUsed = runsOnWorker.reduce((sum, r) => sum + (r.resources?.ram_gib || 0.0), 0);
    const vramUsed = runsOnWorker.reduce((sum, r) => sum + (r.current_vram_gib != null ? r.current_vram_gib : (r.resources?.gpu_vram_gib || 0.0)), 0);
    
    const queuedCount = S.queue.filter(t => t.status === 'queued' && projects.includes(t.project)).length;
    
    const versionBadge = w.version
      ? (w.version_ok === false
          ? `<span style="color:#ef4444;font-size:11px" title="${escHtml(w.version_note||'')}">⚠ ${escHtml(w.version||'')} (schema ${escHtml(String(w.schema_version||'?'))})</span>`
          : (w.version_note
              ? `<span style="color:#f59e0b;font-size:11px" title="${escHtml(w.version_note)}">${escHtml(w.version||'')} ↑</span>`
              : `<span style="font-size:11px;color:var(--text2)">${escHtml(w.version||'')} (schema ${escHtml(String(w.schema_version||'?'))})</span>`))
      : '<span style="font-size:11px;color:var(--text2)">—</span>';
    const syncBtn = `<button data-click="${tqAct([['workerSync', TQ_THIS, String(w.worker_id)]])}" style="font-size:10px;padding:2px 7px;border:none;border-radius:4px;background:#3b82f6;color:#fff;cursor:pointer;margin-left:4px" title="${i18n.t('workers.syncTitle')}">sync</button>`;
    return `
      <tr style="border-bottom:1px solid var(--border)">
        <td style="padding:12px 5px;font-weight:bold;color:var(--text);flex:1 1 100%">${escHtml(w.worker_id)}${syncBtn} <span style="font-weight:normal;color:${statusColor}">${escHtml(w.status)}</span> ${versionBadge}</td>
        <td style="padding:12px 5px"><span style="color:var(--muted);font-size:10px">OS </span>${escHtml(w.os)} ${escHtml(w.arch)}</td>
        <td style="padding:12px 5px"><span style="color:var(--muted);font-size:10px">CPU </span>${coresUsed.toFixed(0)}/${escHtml(String(w.cpu_cores ?? '?'))}</td>
        <td style="padding:12px 5px"><span style="color:var(--muted);font-size:10px">RAM </span>${ramUsed.toFixed(1)}/${_n(w.ram_gib).toFixed(1)} GB</td>
        <td style="padding:12px 5px"><span style="color:var(--muted);font-size:10px">VRAM </span>${vramUsed.toFixed(1)}/${_n(w.gpu_vram_gib).toFixed(1)} GB</td>
        <td style="padding:12px 5px"><span style="color:var(--muted);font-size:10px">tasks </span>${runsOnWorker.length} run / ${queuedCount} queued</td>
        <td class="mob-hide" style="padding:12px 5px;color:var(--text2)">${escHtml(projects.join(', '))}</td>
        <td class="mob-hide" style="padding:12px 5px;color:var(--text2)">${lastSeenStr}</td>
      </tr>
    `;
  } catch (e) {
    console.error('[tq] renderWorkers row failed:', e);
    return `<tr><td colspan="8" style="padding:8px;color:var(--text2)">${escHtml(String(w && w.worker_id))}: render error</td></tr>`;
  } }).join('');
}
