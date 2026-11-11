// Task drawer (info / YAML / history tabs, retry/force/stop/log actions). State lives in S.drawerTask, S.drawerTab,
// S.drawerYamlMtime. Uses app.js globals at call time: tqAct, i18n, badge, res, _fetchJson, _isRestartStatus,
// _progressPct, _lastRunProgressHtml, tqSetPriority, tqSetEnabled, retryTaskNew, forceTask, stopTask, tqQueueActive,
// toggleCardLog, toggleQueueLog, toggleDashLog, toggleHistoryCardLog, toggleHistoryLog, tqOpenLogPanel.
import { S } from '../core/state.js';
import { latest } from '../core/api.js';
import { _isRestartStatus, _requeueLabel } from '../core/nav.js';
import { escHtml, dur, fmt, taskKey, etaText } from '../util/format.js';
import { showToast, openAlert } from './modals.js';
import { _depMeta, _depTagsHtml } from '../views/queue.js';

export function tqFindTask(project, taskId) {
  const hit = (list) => (list || []).find(t => t.project === project && t.task_id === taskId);
  return hit(S.queue) || hit(S.status && S.status.running) || hit(S.history) || { project, task_id: taskId };
}
export function tqOnTaskCellClick(ev, project, taskId) {
  if (ev.target.closest('button, a, input, .dep-tag')) return;
  tqOpenDrawer(project, taskId);
}
/** Inline SVG sparkline for [[ts, value], ...] (no axes; min/max shown next to it).
 * `prev` (optional) is the previous run's series for the same key: drawn dashed underneath on a shared
 * y-scale, each run stretched to the full width (runs differ in length; compare shape, not time). */
export function metricSparkSvg(points, w = 120, h = 24, prev = null) {
  const toVals = ps => (ps || []).map(p => Number(p[1])).filter(v => Number.isFinite(v));
  const vals = toVals(points);
  if (vals.length < 2) return '';
  const pvals = toVals(prev);
  const all = pvals.length >= 2 ? vals.concat(pvals) : vals;
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const line = vs => vs.map((v, i) => `${(i * w / (vs.length - 1)).toFixed(1)},${(h - 2 - ((v - lo) / span) * (h - 4)).toFixed(1)}`).join(' ');
  const under = pvals.length >= 2
    ? `<polyline class="tq-metric-prev-line" fill="none" stroke="currentColor" stroke-opacity="0.45" stroke-width="1" stroke-dasharray="3 2" points="${line(pvals)}"/>`
    : '';
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">${under}<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${line(vals)}"/></svg>`;
}

/** TQ_METRIC series of the run before the drawer task's latest one (once per drawer open; it does not change). */
export async function tqDrawerLoadPrevSeries() {
  const t = S.drawerTask;
  if (!t || !t.run_id) return;
  const live = latest('drawer-prev-series');
  const runs = await _fetchJson(`/api/tasks/${encodeURIComponent(t.project)}/${encodeURIComponent(t.task_id)}/runs?limit=5`, 8000, true);
  const prev = ((runs && runs.runs) || []).find(r => Number(r.run_id) < Number(t.run_id));
  if (!prev || !live()) return;
  const d = await _fetchJson('/api/runs/' + encodeURIComponent(prev.run_id), 8000, true);
  if (!live() || !S.drawerTask || S.drawerTask.task_id !== t.task_id || S.drawerTask.project !== t.project) return;
  const series = d && d.metrics && d.metrics._series;
  S.drawerPrevSeries = series && Object.keys(series).length ? { runId: prev.run_id, series } : null;
  if (S.drawerTab === 'info' && S.drawerSeries) tqPaintDrawerBody();
}

/** Load TQ_METRIC series of the drawer task's latest run (at most every 10 s while it runs). */
export async function tqDrawerLoadSeries(force) {
  const t = S.drawerTask;
  if (!t || !t.run_id) { S.drawerSeries = null; return; }
  if (!force && Date.now() - S.drawerSeriesAt < 10000) return;
  S.drawerSeriesAt = Date.now();
  const live = latest('drawer-series');
  const d = await _fetchJson('/api/runs/' + encodeURIComponent(t.run_id), 8000, true);
  if (!live() || !S.drawerTask || S.drawerTask.run_id !== t.run_id) return;
  const series = d && d.metrics && d.metrics._series;
  S.drawerSeries = series && Object.keys(series).length ? series : null;
  if (S.drawerTab === 'info') tqPaintDrawerBody();
}

export function tqOpenDrawer(project, taskId) {
  S.drawerTask = tqFindTask(project, taskId);
  S.drawerTab = 'info';
  S.drawerYamlMtime = null;
  S.drawerSeries = null;
  S.drawerSeriesAt = 0;
  S.drawerPrevSeries = null;
  const back = document.getElementById('tq-drawer-back');
  if (back) back.hidden = false;
  tqPaintDrawerHead();
  tqPaintDrawerBody();
  tqDrawerLoadSeries(true);
  tqDrawerLoadPrevSeries();
  // If the task isn't in the live caches (e.g. opened from task-stats), fetch full task data.
  // Always refresh detail so stop_reason / stop_summary land for terminal tasks.
  _fetchJson(
    '/api/tasks/' + encodeURIComponent(project) + '/' + encodeURIComponent(taskId),
    8000, true
  ).then(data => {
    if (data && data.task && S.drawerTask && S.drawerTask.task_id === taskId) {
      S.drawerTask = Object.assign({}, S.drawerTask, data.task, { project, task_id: taskId });
      tqPaintDrawerHead();
      tqPaintDrawerBody();
    }
  });
}
export function tqCloseDrawer() {
  const back = document.getElementById('tq-drawer-back');
  if (back) back.hidden = true;
  S.drawerTask = null;
}
export function tqSyncDrawerLive() {
  if (!S.drawerTask) return;
  const fresh = tqFindTask(S.drawerTask.project, S.drawerTask.task_id);
  if (fresh && fresh.status) S.drawerTask = fresh;
  tqPaintDrawerHead();
  if (S.drawerTab === 'info') tqPaintDrawerBody();
  if (S.drawerTask.status === 'running') tqDrawerLoadSeries(false);
}
export function tqPaintDrawerHead() {
  const t = S.drawerTask;
  if (!t) return;
  const idEl = document.getElementById('tq-drawer-id');
  const sub = document.getElementById('tq-drawer-sub');
  const badgeEl = document.getElementById('tq-drawer-badge');
  if (idEl) idEl.textContent = t.task_id || '';
  if (sub) sub.textContent = (t.project || '') + (t.priority != null ? ' · ' + i18n.t('drawer.priority') + ' ' + t.priority : '');
  if (badgeEl) badgeEl.innerHTML = t.status ? badge(t.status, t.last_error_code) : '';
  document.querySelectorAll('.tq-drawer-tabs button').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-dtab') === S.drawerTab);
  });
  const retry = document.getElementById('tq-drawer-retry');
  const force = document.getElementById('tq-drawer-force');
  const stop = document.getElementById('tq-drawer-stop');
  if (retry) {
    retry.disabled = !(_isRestartStatus(t.status) || ['failed','completed'].includes(t.status));
    retry.textContent = _isRestartStatus(t.status) ? i18n.t('drawer.restart') : i18n.t('drawer.retry');
  }
  if (force) {
    const starting = !!(S.forceStarting && S.forceStarting[t.project + '/' + t.task_id]);
    force.disabled = starting || t.status !== 'queued';
    force.textContent = starting ? i18n.t('btn.starting') : i18n.t('drawer.force');
  }
  if (stop) stop.disabled = !(t.status === 'running' || t.status === 'queued');
  const hold = document.getElementById('tq-drawer-hold');
  const unhold = document.getElementById('tq-drawer-unhold');
  if (hold) hold.disabled = t.status !== 'running';
  if (unhold) unhold.disabled = t.status !== 'running';
  const en = document.getElementById('tq-drawer-enable');
  if (en) {
    en.disabled = !t.status || t.status === 'running';
    en.textContent = t.status === 'disabled' ? (i18n.t('ctx.enable')) : (i18n.t('ctx.disable'));
  }
}
export function tqDrawerPrio(delta, absolute) {
  const t = S.drawerTask;
  if (!t) return;
  const inp = document.getElementById('tq-drawer-prio');
  const cur = inp ? parseInt(inp.value, 10) || 0 : (Number(t.priority) || 0);
  const next = absolute ? cur : (Number(t.priority) || 0) + delta;
  tqSetPriority(t.project, t.task_id, next);
}
export function tqDrawerToggleEnabled() {
  const t = S.drawerTask;
  if (!t) return;
  tqSetEnabled(t.project, t.task_id, t.status === 'disabled' ? 1 : 0);
}
export function tqDrawerTab(name) {
  S.drawerTab = name;
  tqPaintDrawerHead();
  tqPaintDrawerBody();
}
export function tqInfoCard(label, value) {
  return `<div class="tq-info-card"><div class="k">${escHtml(label)}</div><div class="v">${value || '—'}</div></div>`;
}
export function tqPaintDrawerBody() {
  const body = document.getElementById('tq-drawer-body');
  const t = S.drawerTask;
  if (!body || !t) return;
  if (S.drawerTab === 'info') {
    // Live sync repaints this card; keep a priority the user is still typing.
    const _prioEl = document.getElementById('tq-drawer-prio');
    const _prioTyping = _prioEl && document.activeElement === _prioEl ? _prioEl.value : null;
    const drawerPct = _progressPct(t);
    const prog = drawerPct == null ? '' : (t.status === 'running'
      ? `<div class="tq-info-card wide"><div class="k">${escHtml(i18n.t('drawer.progress'))}</div><div class="pbar-wrap pbar-3"><div class="pbar" style="width:${drawerPct.toFixed(1)}%"></div></div><div class="v">${drawerPct.toFixed(0)}%</div></div>`
      : `<div class="tq-info-card wide"><div class="k">${escHtml(i18n.t('drawer.progress'))}</div>${_lastRunProgressHtml(t, drawerPct)}</div>`);
    const statusMap = {};
    (S.queue || []).forEach(q => { statusMap[`${q.project}/${q.task_id}`] = q.status; });
    ((S.status && S.status.running) || []).forEach(r => { statusMap[taskKey(r.project, r.task_id)] = 'running'; });
    const depMeta = _depMeta(t, statusMap);
    const depCard = !depMeta.deps.length ? '' : `<div class="tq-info-card wide"><div class="k">${escHtml(i18n.t('drawer.deps'))} · ${depMeta.countLabel}</div><div class="dep-line dep-line-drawer">${_depTagsHtml(t, statusMap)}</div></div>`;
    const stopCards = (t.stop_summary || t.stop_reason || t.stopped_by)
      ? `${t.stop_summary ? tqInfoCard(i18n.t('drawer.stopSummary'), escHtml(t.stop_summary)) : ''}
         ${t.stop_reason && t.stop_reason !== t.stop_summary ? tqInfoCard(i18n.t('drawer.stopReason'), escHtml(t.stop_reason)) : ''}
         ${t.stopped_by ? tqInfoCard(i18n.t('drawer.stoppedBy'), escHtml(t.stopped_by)) : ''}`
      : '';
    // Tasks are mostly stopped for being too slow: show time left, or how much was left at the stop.
    const etaLine = t.status === 'completed' ? '' : etaText(t.eta);
    const etaCard = etaLine
      ? `<div class="tq-info-card wide"><div class="k">${escHtml(i18n.t('drawer.eta'))}</div><div class="v" title="${escHtml(t.eta.basis ? i18n.t('eta.basis.' + t.eta.basis) : '')}">${escHtml(etaLine)}</div></div>`
      : '';
    body.innerHTML = `<div class="tq-info-grid">
      ${tqInfoCard(i18n.t('drawer.status'), t.status ? badge(t.status, t.last_error_code) : '—')}
      ${tqInfoCard(i18n.t('drawer.priority'), `<span style="display:inline-flex;gap:4px;align-items:center">
        <button type="button" class="cfg-btn" data-click="${tqAct([['tqDrawerPrio', -10]])}">−10</button>
        <input id="tq-drawer-prio" type="number" min="0" max="100" value="${t.priority != null ? Number(t.priority) : 0}" style="width:64px"
          data-keydown="${tqAct([['tqDrawerPrio', 0, true]])}" data-keydown-key="Enter">
        <button type="button" class="cfg-btn" data-click="${tqAct([['tqDrawerPrio', 10]])}">+10</button>
        <button type="button" class="cfg-btn" data-click="${tqAct([['tqDrawerPrio', 0, true]])}">${escHtml(i18n.t('btn.set'))}</button></span>`)}
      ${tqInfoCard(i18n.t('queue.thQueued'), escHtml(fmt(t.enqueued_at)))}
      ${tqInfoCard(i18n.t('queue.thStarted'), escHtml(fmt(t.started_at)))}
      ${tqInfoCard(i18n.t('queue.thFinished'), t.finished_at ? escHtml(fmt(t.finished_at)) : '—')}
      ${tqInfoCard(i18n.t('queue.thResources'), res(t.resources, t.peak_ram_gib, t.peak_vram_gib) || '—')}
      ${stopCards}
      <div class="tq-info-card wide"><div class="k">${escHtml(i18n.t('queue.thDescription'))}</div><div class="v">${escHtml(t.description || '—')}</div></div>
      ${depCard}
      ${prog}
      ${etaCard}
      ${S.drawerSeries ? `<div class="tq-info-card wide"><div class="k" style="display:flex;align-items:center;gap:6px">${escHtml(i18n.t('drawer.metrics'))}<button type="button" class="cfg-btn" style="font-size:10px;padding:1px 6px" title="${escHtml(i18n.t('drawer.metricsHelpTitle'))}" data-click="${tqAct([['tqOpenGuide', 'web-ui', 'tq-metric']])}">?</button></div>${
        Object.entries(S.drawerSeries).map(([k, pts]) => {
          const num = v => (v == null ? '' : String(+Number(v).toPrecision(4)));
          const last = pts.length ? pts[pts.length - 1][1] : null;
          const prevPts = S.drawerPrevSeries && S.drawerPrevSeries.series[k];
          const prevLast = prevPts && prevPts.length ? prevPts[prevPts.length - 1][1] : null;
          const prevTxt = prevLast == null ? ''
            : `<span class="tq-metric-prev" title="${escHtml(i18n.t('drawer.metricPrevTitle', {n: S.drawerPrevSeries.runId}))}">${escHtml(i18n.t('drawer.metricPrev', {v: num(prevLast)}))}</span>`;
          return `<div class="tq-metric-row"><span class="tq-metric-key">${escHtml(k)}</span>${metricSparkSvg(pts, 120, 24, prevPts)}<span class="tq-metric-val">${escHtml(num(last))}</span>${prevTxt}</div>`;
        }).join('')}</div>` : ''}
    </div>`;
    if (_prioTyping !== null) {
      const el = document.getElementById('tq-drawer-prio');
      if (el) { el.value = _prioTyping; el.focus(); }
    }
    return;
  }
  if (S.drawerTab === 'yaml') {
    body.innerHTML = `<div style="display:flex;gap:6px;margin-bottom:8px">
      <button type="button" class="cfg-btn" id="tq-yaml-edit" data-click="${tqAct([['tqDrawerToggleYaml']])}">${escHtml(i18n.t('btn.edit'))}</button>
      <button type="button" class="cfg-btn" id="tq-yaml-save" data-click="${tqAct([['tqDrawerSaveYaml']])}" style="display:none">${escHtml(i18n.t('btn.save'))}</button>
      <button type="button" class="cfg-btn" data-click="${tqAct([['tqDrawerCopyYaml']])}">${escHtml(i18n.t('btn.copy'))}</button>
    </div>
    <pre id="tq-yaml-pre" class="tq-yaml-pre">…</pre>
    <textarea id="tq-yaml-editbox" class="tq-yaml-edit" style="display:none"></textarea>
    <div id="tq-yaml-err" style="color:#f87171;font-size:12px;display:none;margin-top:6px"></div>`;
    tqDrawerLoadYaml();
    return;
  }
  body.innerHTML = `<div style="color:var(--muted);font-size:12px">${escHtml(i18n.t('drawer.historyLoading'))}</div>`;
  tqDrawerLoadHistory();
}
export async function tqDrawerLoadYaml() {
  const t = S.drawerTask;
  const pre = document.getElementById('tq-yaml-pre');
  const err = document.getElementById('tq-yaml-err');
  if (!t || !pre) return;
  const live = latest('drawer-yaml');
  try {
    const r = await fetch(`/api/tasks/${encodeURIComponent(t.project)}/${encodeURIComponent(t.task_id)}/yaml`);
    if (!live() || S.drawerTask !== t) return;
    if (!r.ok) {
      pre.textContent = '';
      if (err) { err.style.display = 'block'; err.textContent = 'Error ' + r.status; }
      return;
    }
    const mtime = r.headers.get('X-File-Mtime-Ns');
    const text = await r.text();
    if (!live() || S.drawerTask !== t) return;
    S.drawerYamlMtime = mtime;
    pre.textContent = text;
  } catch (e) {
    if (err) { err.style.display = 'block'; err.textContent = String(e.message || e); }
  }
}
export function tqDrawerToggleYaml() {
  const pre = document.getElementById('tq-yaml-pre');
  const ta = document.getElementById('tq-yaml-editbox');
  const edit = document.getElementById('tq-yaml-edit');
  const save = document.getElementById('tq-yaml-save');
  if (!pre || !ta) return;
  const editing = ta.style.display !== 'none';
  if (!editing) {
    ta.value = pre.textContent;
    ta.style.display = 'block';
    pre.style.display = 'none';
    if (edit) edit.textContent = i18n.t('btn.cancel');
    if (save) save.style.display = '';
  } else {
    ta.style.display = 'none';
    pre.style.display = '';
    if (edit) edit.textContent = i18n.t('btn.edit');
    if (save) save.style.display = 'none';
  }
}
export async function tqDrawerSaveYaml() {
  const t = S.drawerTask;
  const ta = document.getElementById('tq-yaml-editbox');
  const err = document.getElementById('tq-yaml-err');
  const save = document.getElementById('tq-yaml-save');
  if (!t || !ta) return;
  if (err) err.style.display = 'none';
  if (save) { save.disabled = true; save.textContent = i18n.t('btn.saving'); }
  try {
    const hdrs = { 'Content-Type': 'text/plain' };
    if (S.drawerYamlMtime) hdrs['X-If-Mtime-Ns'] = S.drawerYamlMtime;
    const r = await fetch(`/api/tasks/${encodeURIComponent(t.project)}/${encodeURIComponent(t.task_id)}/yaml`, {
      method: 'PUT', headers: hdrs, body: ta.value,
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      if (err) { err.style.display = 'block'; err.textContent = j.error || ('Error ' + r.status); }
    } else {
      const pre = document.getElementById('tq-yaml-pre');
      if (pre) pre.textContent = ta.value;
      const r2 = await fetch(`/api/tasks/${encodeURIComponent(t.project)}/${encodeURIComponent(t.task_id)}/yaml`);
      if (r2.ok) S.drawerYamlMtime = r2.headers.get('X-File-Mtime-Ns');
      tqDrawerToggleYaml();
      showToast(i18n.t('drawer.yamlSaved'), 'ok');
    }
  } catch (e) {
    if (err) { err.style.display = 'block'; err.textContent = String(e.message || e); }
  } finally {
    if (save) { save.disabled = false; save.textContent = i18n.t('btn.save'); }
  }
}
export async function tqDrawerCopyYaml() {
  const pre = document.getElementById('tq-yaml-pre');
  const ta = document.getElementById('tq-yaml-editbox');
  const text = (ta && ta.style.display !== 'none') ? ta.value : (pre ? pre.textContent : '');
  try {
    await navigator.clipboard.writeText(text);
    showToast(i18n.t('btn.copied'), 'ok');
  } catch (e) {
    showToast(String(e.message || e), 'err');
  }
}
export async function tqDrawerLoadHistory() {
  const t = S.drawerTask;
  const body = document.getElementById('tq-drawer-body');
  if (!t || !body || S.drawerTab !== 'history') return;
  const live = latest('drawer-history');
  try {
    const resp = await fetch(`/api/tasks/${encodeURIComponent(t.project)}/${encodeURIComponent(t.task_id)}/runs?limit=5`);
    if (!resp.ok) throw new Error('Error ' + resp.status);
    const data = await resp.json();
    if (!live() || S.drawerTask !== t || S.drawerTab !== 'history') return;
    const runs = (data.runs || []).slice(0, 5);
    if (!runs.length) {
      body.innerHTML = `<div style="color:var(--muted);font-size:12px">${escHtml(i18n.t('drawer.noRuns'))}</div>`;
      return;
    }
    body.innerHTML = runs.map(r => {
      const startedMs = r.started_at ? new Date(r.started_at).getTime() : null;
      const finishedMs = r.finished_at ? new Date(r.finished_at).getTime() : null;
      const elapsed = (startedMs && finishedMs) ? Math.round((finishedMs - startedMs) / 1000) : null;
      return `<div class="tq-hist-row"><span style="color:var(--muted)">#${escHtml(String(r.run_id))}</span>${badge(r.status)}<span>${escHtml(elapsed != null ? dur(elapsed) : '—')}</span><span style="margin-left:auto;color:var(--muted)">${escHtml(fmt(r.started_at))}</span></div>`;
    }).join('');
  } catch (e) {
    body.innerHTML = `<div style="color:#f87171;font-size:12px">${escHtml(String(e.message || e))}</div>`;
  }
}
const _WHY_RESOURCES = [['cpu_cores', 'CPU', 'cores'], ['ram_gib', 'RAM', 'GiB'], ['gpu_vram_gib', 'VRAM', 'GiB']];

/** One "RAM: needs 20 GiB, 10 GiB free" line per resource the task declares (dry-run need vs headroom). */
export function _whyNeedFreeLines(plan) {
  if (!plan || !plan.need || !plan.free) return [];
  const n = v => (Math.round(Number(v || 0) * 10) / 10).toString();
  return _WHY_RESOURCES
    .filter(([k]) => Number(plan.need[k] || 0) > 0)
    .map(([k, label, unit]) => i18n.t('drawer.whyNeedFree', {res: label, need: n(plan.need[k]) + ' ' + unit, free: n(plan.free[k]) + ' ' + unit}));
}

/** "Why not running?": the scheduler's last decision plus a dry-run verdict (GET /api/tasks/<p>/<t>/why). */
export async function tqDrawerWhy() {
  const t = S.drawerTask;
  if (!t) return;
  try {
    const r = await fetch('/api/tasks/' + encodeURIComponent(t.project) + '/' + encodeURIComponent(t.task_id) + '/why');
    const info = await r.json();
    if (!r.ok || !info.ok) throw new Error(info.error || ('HTTP ' + r.status));
    const lines = (info.reasons || []).map(x => '• ' + x);
    if (info.plan && !(info.reasons || []).some(x => String(x).startsWith('dry run'))) lines.push(i18n.t('drawer.whyDryRun') + ': ' + info.plan.verdict + (info.plan.reason ? ' — ' + info.plan.reason : ''));
    lines.push(..._whyNeedFreeLines(info.plan).map(x => '  ' + x));
    openAlert(lines.join('\n'), i18n.t('drawer.whyTitle') + ' ' + t.project + '/' + t.task_id);
  } catch (e) {
    showToast(String(e.message || e), 'err');
  }
}

export function tqDrawerAction(kind) {
  const t = S.drawerTask;
  if (!t) return;
  if (kind === 'log') {
    tqCloseDrawer();
    if (!t.run_id) { showToast(i18n.t('drawer.noLog'), 'err'); return; }
    if (tqQueueActive()) {
      const cards = document.getElementById('queue-mobile-cards');
      const useCards = cards && getComputedStyle(cards).display !== 'none';
      if (useCards && document.getElementById(`qc-log-wrap-${t.run_id}`)) {
        if (!S.expandedLogs[`qc:${t.run_id}`]) toggleCardLog(t.run_id, t.project, t.task_id, t.status === 'running');
        return;
      }
      const rowId = `q-log-row-${t.run_id}`;
      if (document.getElementById(rowId)) {
        if (!S.expandedLogs[`q:${t.run_id}`]) toggleQueueLog(t.run_id, rowId, t.status === 'running');
        else document.getElementById(rowId).scrollIntoView({ block: 'nearest' });
        return;
      }
    }
    const dash = document.getElementById('dashboard');
    if (dash && dash.classList.contains('active') && document.getElementById(`d-log-wrap-${t.run_id}`)) {
      if (!S.expandedLogs[`d:${t.run_id}`]) toggleDashLog(t.run_id, t.project, t.task_id);
      else document.getElementById(`d-log-wrap-${t.run_id}`).scrollIntoView({ block: 'nearest' });
      return;
    }
    const hist = document.getElementById('history');
    if (hist && hist.classList.contains('active')) {
      const cards = document.getElementById('history-mobile-cards');
      const useCards = cards && getComputedStyle(cards).display !== 'none';
      if (useCards && document.getElementById(`hc-log-wrap-${t.run_id}`)) {
        if (!S.expandedLogs[`h:${t.run_id}`]) toggleHistoryCardLog(t.run_id, t.status === 'running');
        return;
      }
      const rowId = `h-log-row-${t.run_id}`;
      if (document.getElementById(rowId)) {
        if (!S.expandedLogs[`h:${t.run_id}`]) toggleHistoryLog(t.run_id, rowId, t.status === 'running');
        else document.getElementById(rowId).scrollIntoView({ block: 'nearest' });
        return;
      }
    }
    tqOpenLogPanel(t.run_id, t.project, t.task_id, t.status === 'running');
    return;
  }
  const btn = document.getElementById('tq-drawer-' + kind);
  if (kind === 'retry') retryTaskNew(t.project, t.task_id, btn);
  else if (kind === 'force') forceTask(btn, t.project, t.task_id);
  else if (kind === 'stop') stopTask(btn, t.project, t.task_id);
  else if (kind === 'hold') holdTask(btn, t.project, t.task_id);
  else if (kind === 'unhold') unholdTask(btn, t.project, t.task_id);
}
