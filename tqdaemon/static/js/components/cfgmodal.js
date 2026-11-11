// Config modal (host limits + set defaults + per-project overrides) and JSON helpers.
// Uses app.js globals at call time (i18n, openAlert, refresh, _renderCfgProjectSets).
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';
import { showToast } from './modals.js';

const LIMIT_FIELDS = [
  ['max_tasks', ''],
  ['min_tasks', ''],
  ['max_cpu_cores', ''],
  ['max_ram_gib', '0.5'],
  ['max_gpu_vram_gib', '0.5'],
  ['max_gpu_hours_day', '0.5'],
];

function _normalizeLimitsPayload(raw) {
  // New shape: { projects: { name: { override, effective, inherited_from, set } }, sets: {...} }
  // Legacy shape: { name: { max_tasks, ... } }
  if (raw && raw.projects && typeof raw.projects === 'object') {
    return {
      projects: raw.projects,
      sets: raw.sets || {},
    };
  }
  const projects = {};
  for (const [name, lim] of Object.entries(raw || {})) {
    const override = {};
    for (const [f] of LIMIT_FIELDS) override[f] = Number((lim && lim[f]) || 0);
    projects[name] = {
      override,
      effective: { ...override },
      inherited_from: Object.fromEntries(LIMIT_FIELDS.map(([f]) => [f, override[f] ? 'project' : 'none'])),
      set: null,
    };
  }
  return { projects, sets: {} };
}

function _limitInputs(kind, key, values, placeholders, sources) {
  return LIMIT_FIELDS.map(([field, step]) => {
    const val = values && values[field] != null ? values[field] : 0;
    const ph = placeholders && placeholders[field] != null ? placeholders[field] : '';
    const src = sources && sources[field] ? sources[field] : '';
    const s = step ? ` step="${step}"` : '';
    const title = src && src !== 'none' && src !== 'project' ? ` title="inherited ${escHtml(src)}"` : '';
    const phAttr = ph !== '' && Number(val) === 0 ? ` placeholder="${escHtml(String(ph))}"` : '';
    return `<td style="padding:4px 6px"><input type="number" min="0" max="9999"${s} style="width:70px" data-kind="${kind}" data-key="${escHtml(key)}" data-field="${field}" value="${val}"${phAttr}${title}></td>`;
  }).join('');
}

function _renderCfgLimitsPanel(payload) {
  const container = document.getElementById('cfg-project-limits');
  if (!container) return;
  const projects = S.status && S.status.projects ? S.status.projects : Object.keys(payload.projects || {});
  if (!projects.length && !Object.keys(payload.sets || {}).length) {
    container.innerHTML = `<div style="color:var(--muted);font-size:12px">${i18n.t('config.noProjects')}</div>`;
    return;
  }

  const selected = S._cfgSetFocus || '';
  const setPaths = Object.keys(payload.sets || {}).sort();
  // Also include tree paths from project membership even if no limits yet.
  const map = (S.status && S.status.project_set_map) || S.projectSetMap || {};
  for (const p of projects) {
    const sp = map[p] || (payload.projects[p] && payload.projects[p].set);
    if (!sp) continue;
    const parts = String(sp).split('/');
    for (let i = 1; i <= parts.length; i++) {
      const path = parts.slice(0, i).join('/');
      if (!setPaths.includes(path)) setPaths.push(path);
    }
  }
  setPaths.sort();

  let html = '<div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">';
  html += '<div style="min-width:140px;max-width:220px">';
  html += '<div style="font-size:11px;color:var(--muted);margin-bottom:4px">' + i18n.t('cfg.selectSet') + '</div>';
  html += `<button type="button" class="filter-chip${!selected ? ' active' : ''}" data-click="${tqAct([['tqCfgFocusSet', '']])}">${i18n.t('cfg.allProjects')}</button>`;
  for (const path of setPaths) {
    const depth = path.split('/').length - 1;
    const pad = depth * 10;
    html += `<button type="button" class="filter-chip${selected === path ? ' active' : ''}" style="margin-left:${pad}px" data-click="${tqAct([['tqCfgFocusSet', path]])}">${escHtml(path)}</button>`;
  }
  html += '</div><div style="flex:1;min-width:280px">';

  if (selected) {
    const setLim = (payload.sets && payload.sets[selected]) || {};
    const members = projects.filter(p => {
      const sp = map[p] || (payload.projects[p] && payload.projects[p].set);
      return sp === selected || (sp && String(sp).startsWith(selected + '/'));
    });
    html += `<div style="font-size:12px;margin-bottom:6px"><b>${i18n.t('cfg.setDefaults')}</b> ${escHtml(selected)}`
      + ` <span style="color:var(--muted)">${i18n.t('cfg.appliesTo', {n: members.length})}</span></div>`;
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:12px"><thead><tr style="text-align:left">'
      + '<th style="padding:4px 6px">set</th>'
      + LIMIT_FIELDS.map(([f]) => `<th style="padding:4px 6px">${escHtml(f)}</th>`).join('')
      + '</tr></thead><tbody>';
    html += `<tr><td style="padding:4px 6px;font-weight:500">${escHtml(selected)}</td>`
      + _limitInputs('set', selected, setLim, null, null)
      + '</tr></tbody></table>';

    html += '<div style="font-size:12px;margin-bottom:6px"><b>' + i18n.t('cfg.memberOverrides') + '</b> <span style="color:var(--muted)">' + i18n.t('cfg.inheritSet') + '</span></div>';
    html += _projectsTable(members, payload, true);
  } else {
    html += '<div style="font-size:12px;margin-bottom:6px"><b>' + i18n.t('cfg.projectOverrides') + '</b> <span style="color:var(--muted)">' + i18n.t('cfg.inheritSetGlobal') + '</span></div>';
    html += _projectsTable(projects, payload, true);
  }
  html += '</div></div>';
  container.innerHTML = html;
}

function _projectsTable(names, payload, withInherit) {
  if (!names.length) {
    return `<div style="color:var(--muted);font-size:12px">${i18n.t('config.noProjects')}</div>`;
  }
  let html = '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="text-align:left">'
    + `<th style="padding:4px 6px">${i18n.t('config.thProject')}</th>`
    + `<th style="padding:4px 6px">${i18n.t('config.thMaxTasks')}</th>`
    + `<th style="padding:4px 6px">${i18n.t('config.thMinTasks')}</th>`
    + `<th style="padding:4px 6px">${i18n.t('config.thMaxCpuCores')}</th>`
    + `<th style="padding:4px 6px">${i18n.t('config.thMaxRamGib')}</th>`
    + `<th style="padding:4px 6px">${i18n.t('config.thMaxGpuVramGib')}</th>`
    + `<th style="padding:4px 6px">${i18n.t('config.thMaxGpuHoursDay')}</th>`
    + '</tr></thead><tbody>';
  for (const proj of names) {
    const entry = (payload.projects && payload.projects[proj]) || {};
    const ov = entry.override || {};
    const eff = entry.effective || ov;
    const src = entry.inherited_from || {};
    const placeholders = withInherit ? eff : null;
    html += `<tr><td style="padding:4px 6px;font-weight:500">${escHtml(proj)}`
      + (entry.set ? `<div style="font-size:10px;color:var(--muted)">${escHtml(entry.set)}</div>` : '')
      + `</td>`
      + _limitInputs('proj', proj, ov, placeholders, src)
      + '</tr>';
  }
  html += '</tbody></table>';
  return html;
}

export function tqCfgFocusSet(path) {
  S._cfgSetFocus = path || '';
  if (window.__tqCfgLimitsPayload) _renderCfgLimitsPanel(window.__tqCfgLimitsPayload);
}

export async function openCfg() {
  const numOr = (v, d) => (v === 0 || v === '0') ? 0 : (v || d);
  document.getElementById('cfg-cpu').value = numOr(S.cfg.cpu_limit_pct, 50);
  document.getElementById('cfg-gpu').value = numOr(S.cfg.gpu_limit_pct, 100);
  document.getElementById('cfg-ram').value = numOr(S.cfg.ram_limit_pct, 80);
  document.getElementById('cfg-stop').value = numOr(S.cfg.graceful_stop_sec, 30);
  document.getElementById('cfg-frozen').value = numOr(S.cfg.frozen_threshold_sec, 600);
  document.getElementById('cfg-max-tasks').value = numOr(S.cfg.max_running_tasks, 0);
  document.getElementById('cfg-limit-policy').value = S.cfg.limit_decrease_policy || 'ignore';
  document.getElementById('cfg-max-cpu').value = numOr(S.cfg.max_cpu_cores, 0);
  document.getElementById('cfg-max-ram').value = numOr(S.cfg.max_ram_gib, 0);
  document.getElementById('cfg-vram-cap').value = numOr(S.cfg.gpu_vram_limit_gib, 0);

  let raw = {};
  let projLimitsOk = false;
  try {
    const r = await fetch('/api/project-limits');
    if (r.ok) {
      raw = await r.json();
      projLimitsOk = true;
    }
  } catch (e) {}
  window.__tqCfgProjLimitsOk = projLimitsOk;
  const payload = _normalizeLimitsPayload(raw);
  // Ensure every registered project appears even with empty overrides.
  const projects = S.status && S.status.projects ? S.status.projects : [];
  for (const p of projects) {
    if (!payload.projects[p]) {
      const z = Object.fromEntries(LIMIT_FIELDS.map(([f]) => [f, 0]));
      payload.projects[p] = {
        override: { ...z },
        effective: { ...z },
        inherited_from: Object.fromEntries(LIMIT_FIELDS.map(([f]) => [f, 'none'])),
        set: ((S.status && S.status.project_set_map) || S.projectSetMap || {})[p] || null,
      };
    }
  }
  window.__tqCfgLimitsPayload = payload;
  if (!S._cfgSetFocus) S._cfgSetFocus = '';
  _renderCfgLimitsPanel(payload);
  if (typeof _renderCfgProjectSets === 'function') _renderCfgProjectSets();
  document.getElementById('cfg-modal').classList.add('open');
}
export function closeCfg() { document.getElementById('cfg-modal').classList.remove('open'); }
export function _postJson(url, body) { return _sendJson('POST', url, body); }

/**
 * The common "request -> success toast -> follow-up, or error toast" chain. Takes the request promise
 * (e.g. `_postJson(url)`); resolves to the response data, or `false` after showing the error.
 */
export function toastResult(request, okMsg, after) {
  const toast = (m, k) => (typeof window !== 'undefined' && typeof window.showToast === 'function' ? window.showToast : showToast)(m, k);
  return Promise.resolve(request)
    .then(data => { if (okMsg) toast(okMsg, 'ok'); if (after) after(data); return data; })
    .catch(err => { toast(String((err && err.message) || err), 'err'); return false; });
}

export async function saveCfg() {
  const byProj = {};
  const bySet = {};
  for (const inp of document.querySelectorAll('#cfg-project-limits input[data-field]')) {
    const field = inp.dataset.field;
    if (!field) continue;
    const kind = inp.dataset.kind;
    if (kind === 'set') {
      const path = inp.dataset.key;
      if (!path) continue;
      if (!bySet[path]) bySet[path] = { set: path };
      bySet[path][field] = +inp.value;
      continue;
    }
    // kind=proj (new UI) or legacy data-proj inputs (tests / old DOM)
    const proj = kind === 'proj' ? inp.dataset.key : inp.dataset.proj;
    if (!proj) continue;
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
    if (window.__tqCfgProjLimitsOk) {
      const posts = [];
      for (const [proj, limits] of Object.entries(byProj)) {
        posts.push(_postJson(`/api/project-limits/${encodeURIComponent(proj)}`, limits));
      }
      for (const body of Object.values(bySet)) {
        posts.push(_postJson('/api/set-limits', body));
      }
      if (posts.length) await Promise.all(posts);
    }
  } catch (e) {
    openAlert(String(e.message || e), i18n.t('config.title'));
    return;
  }
  closeCfg();
  try {
    const rfn = typeof window !== 'undefined' ? window.refresh : null;
    if (typeof rfn === 'function') await rfn();
  } catch (_) {}
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
