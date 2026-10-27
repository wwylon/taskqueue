// Config modal (limits + per-project limits) and the JSON request helpers (_sendJson/_postJson).
// Uses app.js globals at call time (i18n, openAlert, refresh).
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';
import { showToast } from './modals.js';

// ---- Config modal ----

export async function openCfg() {
  document.getElementById('cfg-cpu').value = S.cfg.cpu_limit_pct || 50;
  document.getElementById('cfg-gpu').value = S.cfg.gpu_limit_pct || 100;
  document.getElementById('cfg-ram').value = S.cfg.ram_limit_pct || 80;
  document.getElementById('cfg-stop').value = S.cfg.graceful_stop_sec || 30;
  document.getElementById('cfg-frozen').value = S.cfg.frozen_threshold_sec || 600;
  document.getElementById('cfg-max-tasks').value = S.cfg.max_running_tasks || 0;
  document.getElementById('cfg-limit-policy').value = S.cfg.limit_decrease_policy || 'ignore';
  document.getElementById('cfg-max-cpu').value = S.cfg.max_cpu_cores || 0;
  document.getElementById('cfg-max-ram').value = S.cfg.max_ram_gib || 0;
  document.getElementById('cfg-vram-cap').value = S.cfg.gpu_vram_limit_gib || 0;

  // Build per-project limits table
  let projLimits = {};
  try {
    const r = await fetch('/api/project-limits');
    projLimits = await r.json();
  } catch(e) {}
  const projects = S.status && S.status.projects ? S.status.projects : [];
  const container = document.getElementById('cfg-project-limits');
  if (projects.length === 0) {
    container.innerHTML = `<div style="color:var(--muted);font-size:12px">${i18n.t('config.noProjects')}</div>`;
  } else {
    let html = '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="text-align:left">'
      + `<th style="padding:4px 6px">${i18n.t('config.thProject')}</th>`
      + `<th style="padding:4px 6px">${i18n.t('config.thMaxTasks')}</th>`
      + `<th style="padding:4px 6px">${i18n.t('config.thMinTasks')}</th>`
      + `<th style="padding:4px 6px">${i18n.t('config.thMaxCpuCores')}</th>`
      + `<th style="padding:4px 6px">${i18n.t('config.thMaxRamGib')}</th>`
      + `<th style="padding:4px 6px">${i18n.t('config.thMaxGpuVramGib')}</th>`
      + `<th style="padding:4px 6px">${i18n.t('config.thMaxGpuHoursDay')}</th>`
      + '</tr></thead><tbody>';
    for (const proj of projects) {
      const lim = projLimits[proj] || {};
      const inp = (field, step) => {
        const val = lim[field] || 0;
        const s = step ? ` step="${step}"` : '';
        return `<input type="number" min="0" max="9999"${s} style="width:70px" data-proj="${escHtml(proj)}" data-field="${field}" value="${val}">`;
      };
      html += `<tr><td style="padding:4px 6px;font-weight:500">${escHtml(proj)}</td>`
        + `<td style="padding:4px 6px">${inp('max_tasks','')}</td>`
        + `<td style="padding:4px 6px">${inp('min_tasks','')}</td>`
        + `<td style="padding:4px 6px">${inp('max_cpu_cores','')}</td>`
        + `<td style="padding:4px 6px">${inp('max_ram_gib','0.5')}</td>`
        + `<td style="padding:4px 6px">${inp('max_gpu_vram_gib','0.5')}</td>`
        + `<td style="padding:4px 6px">${inp('max_gpu_hours_day','0.5')}</td></tr>`;
    }
    html += '</tbody></table>';
    container.innerHTML = html;
  }
  document.getElementById('cfg-modal').classList.add('open');
}
export function closeCfg() { document.getElementById('cfg-modal').classList.remove('open'); }
export function _postJson(url, body) { return _sendJson('POST', url, body); }

/**
 * The common "request -> success toast -> follow-up, or error toast" chain. Takes the request promise
 * (e.g. `_postJson(url)`); resolves to the response data, or `false` after showing the error.
 */
export function toastResult(request, okMsg, after) {
  // Resolve the toast at call time (window.showToast is the same function on the page; tests replace it).
  const toast = (m, k) => (typeof window !== 'undefined' && typeof window.showToast === 'function' ? window.showToast : showToast)(m, k);
  return Promise.resolve(request)
    .then(data => { if (okMsg) toast(okMsg, 'ok'); if (after) after(data); return data; })
    .catch(err => { toast(String((err && err.message) || err), 'err'); return false; });
}

export async function saveCfg() {
  // Collect per-project limits from table inputs
  const projInputs = document.querySelectorAll('#cfg-project-limits input[data-proj]');
  const byProj = {};
  for (const inp of projInputs) {
    const proj = inp.dataset.proj, field = inp.dataset.field;
    if (!byProj[proj]) byProj[proj] = {};
    byProj[proj][field] = +inp.value;
  }
  try {
    await _postJson('/api/config', {
      cpu_limit_pct: +document.getElementById('cfg-cpu').value,
      gpu_limit_pct: +document.getElementById('cfg-gpu').value,
      ram_limit_pct: +document.getElementById('cfg-ram').value,
      graceful_stop_sec: +document.getElementById('cfg-stop').value,
      frozen_threshold_sec: +document.getElementById('cfg-frozen').value,
      max_running_tasks: +document.getElementById('cfg-max-tasks').value,
      limit_decrease_policy: document.getElementById('cfg-limit-policy').value,
      max_cpu_cores: +document.getElementById('cfg-max-cpu').value,
      max_ram_gib: +document.getElementById('cfg-max-ram').value,
      gpu_vram_limit_gib: +document.getElementById('cfg-vram-cap').value,
    });
    await Promise.all(Object.entries(byProj).map(([proj, limits]) =>
      _postJson(`/api/project-limits/${encodeURIComponent(proj)}`, limits)
    ));
  } catch (e) {
    openAlert(String(e.message || e), i18n.t('config.title'));
    return;
  }
  closeCfg(); refresh();
}

export async function _sendJson(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: {'Content-Type': 'application/json'},
    body: body == null ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(data.error || (r.status + ' ' + url));
  if (data.ok === false) throw new Error(data.error || 'request failed');
  return data;
}
