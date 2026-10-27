// Running-task metrics: ETA, CPU formatting, usage/log-issue chips and in-place live patching of the dashboard gauges
// and run cards. Uses app.js globals at call time (i18n, tqAct, dur, tqRecordSparks, getResourceModalRunId, ...).
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';

/**
 * Seconds left for a running task, or null. Uses the whole running time — earlier runs it resumed
 * (prior_elapsed_sec) plus this run — so a restart does not reset the rate estimate.
 */
export function _etaRemainingSec(t, elapsedSec, pct) {
  const el = (elapsedSec != null ? elapsedSec : t.elapsed_sec) || 0;
  if (!(pct > 0) || !el) return null;
  const total = el + (t.prior_elapsed_sec || 0);
  return total / (pct / 100) - total;
}

export function fmtCpu(pct) {
  return S.cpuTotalCores > 0 ? (pct / 100 * S.cpuTotalCores).toFixed(1) + ' cores' : pct.toFixed(0) + '%';
}

/** Fields that warrant rebuilding run cards / gauge chrome — not live RAM/CPU/progress. */
export function _runningIdentity(running, cfg) {
  const ft = parseFloat((cfg && cfg.frozen_threshold_sec) || 600);
  return JSON.stringify((running || []).map(t => ({
    project: t.project,
    task_id: t.task_id,
    run_id: t.run_id,
    status: t.status,
    worker_id: t.worker_id || null,
    isFrozen: (t.idle_sec || 0) > ft,
    pctKnown: t.progress_pct != null,
  })));
}

export function _gaugeStructureKey(s, cfg) {
  const sys = (s && s.system) || {};
  cfg = cfg || {};
  return JSON.stringify({
    cpu_total: sys.cpu_total_cores,
    ram_total: sys.ram_total_gib,
    gpu_total: sys.gpu_vram_total_gib,
    gpu_name: sys.gpu_name,
    cpu_limit_pct: cfg.cpu_limit_pct,
    gpu_limit_pct: cfg.gpu_limit_pct,
    ram_limit_pct: cfg.ram_limit_pct,
  });
}

export function _gpuBadgeHtml(t) {
  if (!t || !t.requires_gpu) return '';
  const g = t.resources && t.resources.gpu_vram_gib;
  const label = (g != null && g !== '') ? ('GPU ' + g + 'G') : 'GPU';
  return `<span class="gpu-badge" title="This task requests GPU VRAM">${escHtml(String(label))}</span>`;
}

export function _logChipsHtml(t, ctx) {
  const log = (t && t.log) || {};
  const w = log.warnings || 0, e = log.errors || 0, o = log.other || 0;
  if (!w && !e && !o) return '';
  const title = 'errors: ERROR, FATAL, CRITICAL, REJECT · warnings: WARN/WARNING · other: LOW SCORE';
  const runId = (ctx && ctx.runId != null) ? ctx.runId : (t && t.run_id);
  const panel = ctx && ctx.panel;
  const clickable = !!(runId && panel);
  const project = (ctx && ctx.project) || (t && t.project) || '';
  const taskId = (ctx && ctx.taskId) || (t && t.task_id) || '';
  const rowId = (ctx && ctx.rowId) || (runId ? `q-log-row-${runId}` : '');
  const isRunning = (ctx && ctx.isRunning) ? 1 : 0;
  const chip = (label, color, kind) => {
    const onclick = clickable
      ? ` data-click="${tqAct([['openLogIssue', runId, panel, kind, String(project), String(taskId), String(rowId), isRunning]])}" data-click-stop`
      : '';
    const cls = clickable ? ' class="log-issue-chip"' : '';
    return `<span${cls} style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:2px 8px;white-space:nowrap" title="${title}"${onclick}>` +
      `<span style="color:${color};font-weight:600;font-size:11px">${label}</span></span>`;
  };
  const parts = [];
  if (w) parts.push(chip(w + ' warn', 'var(--warn)', 'warn'));
  if (e) parts.push(chip(e + ' err', 'var(--fail)', 'err'));
  if (o) parts.push(chip(o + ' issue', 'var(--muted)', 'other'));
  return parts.join('');
}

export function _usageHtml(t, ctx) {
  const cpu = t.current_cpu_pct;
  const peakCpu = t.peak_cpu_pct;
  const ram = t.current_ram_gib;
  const vram = t.current_vram_gib;
  const peakRam = t.peak_ram_gib;
  const peakVram = t.peak_vram_gib;
  const chip = (label, val, peak, color) =>
    `<span style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:2px 8px;white-space:nowrap">` +
    `<span style="color:var(--muted);font-size:10px">${label} </span>` +
    `<span style="color:${color};font-weight:600">${val}</span>` +
    (peak ? `<span style="color:var(--muted);font-size:10px"> /${peak} pk</span>` : '') +
    `</span>`;
  const parts = [];
  if (cpu != null) {
    const coresUsed = fmtCpu(cpu);
    const peakStr = peakCpu != null ? fmtCpu(peakCpu) : null;
    const cpuTitle = S.cpuTotalCores > 0 ? `${cpu.toFixed(1)}% of system CPU (${S.cpuTotalCores} logical cores)` : '';
    parts.push(`<span style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:2px 8px;white-space:nowrap" title="${cpuTitle}">` +
      `<span style="color:var(--muted);font-size:10px">CPU </span>` +
      `<span style="color:${cpu > 80 ? 'var(--warn)' : 'var(--text)'};font-weight:600">${coresUsed}</span>` +
      (peakStr ? `<span style="color:var(--muted);font-size:10px"> /${peakStr} pk</span>` : '') +
      `</span>`);
  } else if (peakCpu != null) {
    const peakStr = fmtCpu(peakCpu);
    parts.push(chip('CPU pk', peakStr, null, 'var(--text2)'));
  }
  if (ram != null)
    parts.push(chip('RAM', `${ram.toFixed(1)} GiB`, peakRam != null ? peakRam.toFixed(1) + ' GiB' : null, ram > 0.5 ? 'var(--text)' : 'var(--text2)'));
  else if (peakRam != null)
    parts.push(chip('RAM pk', `${peakRam.toFixed(1)} GiB`, null, 'var(--text2)'));
  const wantVram = !!t.requires_gpu || (vram != null && vram > 0) || (peakVram != null && peakVram > 0);
  if (wantVram) {
    if (vram != null)
      parts.push(chip('VRAM', `${vram.toFixed(1)} GiB`, (peakVram != null && peakVram > 0) ? peakVram.toFixed(1) + ' GiB' : null, '#a855f7'));
    else if (peakVram != null && peakVram > 0)
      parts.push(chip('VRAM pk', `${peakVram.toFixed(1)} GiB`, null, '#a855f7'));
    else
      parts.push(chip('VRAM', '0.0 GiB', null, '#a855f7'));
  }
  const gpu = t.current_gpu_util_pct;
  const avgGpu = t.avg_gpu_util_pct;
  if (gpu != null)
    parts.push(chip('GPU', `${gpu.toFixed(0)}%`, avgGpu != null ? avgGpu.toFixed(0) + '% avg' : null, '#f472b6'));
  else if (avgGpu != null)
    parts.push(chip('GPU avg', `${avgGpu.toFixed(0)}%`, null, '#f472b6'));
  const logCtx = Object.assign({
    runId: t.run_id,
    project: t.project,
    taskId: t.task_id,
  }, ctx || {});
  const logChips = _logChipsHtml(t, logCtx);
  if (logChips) parts.push(logChips);
  const isLive = (S.status && S.status.running || []).some(r => r.run_id === t.run_id);
  return parts.length ? parts.join('') : `<span style="color:var(--muted);font-size:10px">${isLive ? i18n.t('status.usagePending') : '—'}</span>`;
}

export function _computeGaugeModel() {
  const sys = (S.status && S.status.system) || {};
  const cfg = S.cfg || {};
  const cpuLimit = (sys.cpu_total_cores || 1) * (cfg.cpu_limit_pct || 50) / 100;
  const gpuLimit = (sys.gpu_vram_total_gib || 0) * (cfg.gpu_limit_pct || 100) / 100;
  const ramLimit = (sys.ram_total_gib || 0) * (cfg.ram_limit_pct || 50) / 100;
  const ramUsed = sys.ram_used_gib || 0;
  const ramTotal = sys.ram_total_gib || 0;
  let cpuUsed = 0, gpuUsed = 0;
  for (const t of ((S.status && S.status.running) || [])) {
    const r = t.resources || {};
    cpuUsed += r.cpu_cores || 1;
    gpuUsed += (t.current_vram_gib != null ? t.current_vram_gib : (r.gpu_vram_gib || 0));
  }
  const ramPct = ramTotal > 0 ? Math.min(100, ramUsed / ramTotal * 100) : 0;
  const ramColor = ramPct > 85 ? 'var(--fail)' : ramPct > 60 ? 'var(--warn)' : 'var(--ok)';
  return { sys, cfg, cpuLimit, gpuLimit, ramLimit, ramUsed, ramTotal, cpuUsed, gpuUsed, ramPct, ramColor };
}

/** In-place gauge + run-card live fields — avoids innerHTML wipe that flashes the UI. */
export function _patchDashboardLive() {
  const g = _computeGaugeModel();
  const setText = (sel, text) => { const el = document.querySelector(sel); if (el) el.textContent = text; };
  const setWidth = (sel, pct) => { const el = document.querySelector(sel); if (el) el.style.width = pct + '%'; };
  const setColor = (sel, color) => { const el = document.querySelector(sel); if (el) el.style.color = color; };

  if (document.querySelector('#gauges [data-gauge="cpu"]')) {
    setText('#gauges [data-gauge="cpu"] [data-g="val"]', `${g.cpuUsed.toFixed(0)} / ${g.cpuLimit.toFixed(0)}`);
    setText('#gauges [data-gauge="cpu"] [data-g="sub"]', i18n.t('gauge.totalCores', {n: g.sys.cpu_total_cores || 0}));
    setWidth('#gauges [data-gauge="cpu"] [data-g="fill"]', Math.min(100, g.cpuUsed / Math.max(g.cpuLimit, 1) * 100));

    setText('#gauges [data-gauge="gpu"] [data-g="val"]', `${g.gpuUsed.toFixed(1)} / ${g.gpuLimit.toFixed(1)} GiB`);
    setText('#gauges [data-gauge="gpu"] [data-g="sub"]', g.sys.gpu_name || i18n.t('gauge.noGpu'));
    setWidth('#gauges [data-gauge="gpu"] [data-g="fill"]', g.gpuLimit ? Math.min(100, g.gpuUsed / g.gpuLimit * 100) : 0);

    setText('#gauges [data-gauge="ram"] [data-g="val"]', `${g.ramUsed.toFixed(1)} / ${g.ramLimit.toFixed(0)} GiB`);
    setColor('#gauges [data-gauge="ram"] [data-g="val"]', g.ramColor);
    setText('#gauges [data-gauge="ram"] [data-g="sub"]', `${g.ramTotal.toFixed(0)} GiB total · queue limit ${(g.cfg.ram_limit_pct || 50)}%`);
    setWidth('#gauges [data-gauge="ram"] [data-g="fill"]', g.ramPct);

    setText('#gauges [data-gauge="queued"] [data-g="val"]', String(S.status.queued_count || 0));
    setText('#gauges [data-gauge="queued"] [data-g="sub"]', `${((S.status && S.status.running) || []).length} running`);

    const rss = S.status.daemon_rss_mb;
    setText('#gauges [data-gauge="daemon"] [data-g="val"]', rss != null ? rss.toFixed(0) + ' MB' : '—');
    setText('#gauges [data-gauge="daemon"] [data-g="sub"]',
      (S.status.daemon_cpu_pct != null ? 'CPU ' + S.status.daemon_cpu_pct + '%' : '') + ' RSS');
    setWidth('#gauges [data-gauge="daemon"] [data-g="fill"]', Math.min(100, (rss || 0) / 5));
    tqRecordSparks();
  }

  const running = (S.status && S.status.running) || [];
  running.forEach(t => {
    if (!t.run_id) return;

    const elapsedEl = document.querySelector(`[data-elapsed-run-id="${t.run_id}"]`);
    if (elapsedEl) elapsedEl.textContent = dur(t.elapsed_sec);

    const etaEl = document.querySelector(`[data-eta-run-id="${t.run_id}"]`);
    if (etaEl && t.progress_pct != null) {
      const pct = t.progress_pct;
      let eta = '';
      const rem = _etaRemainingSec(t, t.elapsed_sec, pct);
      if (rem != null) eta = ` · ETA ~${dur(rem)}`;
      etaEl.textContent = `${pct.toFixed(0)}%${eta}`;
    }

    const pbar = document.querySelector(`[data-pbar-run-id="${t.run_id}"]`);
    if (pbar && t.progress_pct != null) pbar.style.width = t.progress_pct + '%';

    const logEl = document.querySelector(`[data-log-run-id="${t.run_id}"]`);
    if (logEl) logEl.textContent = t.last_log_line ? t.last_log_line : i18n.t('log.noOutput');

    const lastTsEl = document.querySelector(`[data-last-ts-run-id="${t.run_id}"]`);
    if (lastTsEl) {
      const idleSec = t.idle_sec || 0;
      lastTsEl.textContent = idleSec > 5 ? ` · last output ${dur(idleSec)} ago` : '\u00a0';
    }

    const metricsEl = document.getElementById(`dash-metrics-${t.run_id}`);
    if (metricsEl) {
      const html = _usageHtml(t, { panel: 'd' });
      if (metricsEl.dataset.lastHtml !== html) {
        metricsEl.innerHTML = html;
        metricsEl.dataset.lastHtml = html;
      }
    }
  });

  // Keep open metrics modal in sync without wiping unchanged DOM (avoids flash).
  const _mrid = getResourceModalRunId();
  if (_mrid != null) {
    const modal = document.getElementById('resource-modal');
    if (modal && modal.style.display !== 'none') {
      const cur = running.find(r => r.run_id === _mrid);
      if (cur) {
        const body = document.getElementById('resource-modal-body');
        if (body) {
          const html = _resourceModalBodyHtml(cur);
          if (body.dataset.lastHtml !== html) {
            body.innerHTML = html;
            body.dataset.lastHtml = html;
          }
        }
      }
    }
  }
}

export function _updateRunningTimes() { _patchDashboardLive(); }
