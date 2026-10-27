// Resources tab (cluster usage, limits editor, hardware). Uses app.js globals at call time: tqAct, i18n, _postJson,
// openAlert, refresh.
import { S } from '../core/state.js';
import { escHtml } from '../util/format.js';

// ---- I-25/I-27: Resources panel ----

export function toggleResPanel() {
  const body = document.getElementById('res-panel-body');
  const arrow = document.getElementById('res-toggle-arrow');
  if (!body) return;
  const open = body.classList.toggle('open');
  if (arrow) arrow.textContent = open ? '▼' : '▶';
}

export function renderClusterUsage(cap) {
  const el = document.getElementById('cluster-usage-bars');
  if (!el || !S.status) return;
  const running = S.status.running || [];
  const cpu = running.reduce((s,t) => s+(t.current_cpu_pct||0), 0);
  const ram = running.reduce((s,t) => s+(t.current_ram_gib||0), 0);
  const vram = running.reduce((s,t) => s+(t.current_vram_gib||0), 0);
  const cpuLim = cap ? (cap.cpu_cores_limit||0) * 100 : 0;
  const ramLim = cap ? (cap.ram_gib_limit||0) : 0;
  const vramLim = cap ? (cap.gpu_vram_gib_limit||0) : 0;

  function clusterBar(label, used, limit, fmt) {
    const pct = limit > 0 ? Math.min(100, used/limit*100) : 0;
    const color = pct >= 90 ? 'var(--fail)' : pct >= 70 ? 'var(--warn)' : 'var(--ok)';
    const limitStr = limit > 0 ? fmt(limit) : i18n.t('status.unlimited');
    return `<div class="res-bar-row">
      <span class="res-bar-label">${label}</span>
      <div class="res-bar-track"><div class="res-bar-fill" style="width:${pct.toFixed(1)}%;background:${color}"></div></div>
      <span class="res-bar-value">${fmt(used)} / ${limitStr}</span>
    </div>`;
  }
  el.innerHTML = `<div class="res-bars">
    ${clusterBar('CPU %', cpu, cpuLim, v => v.toFixed(0)+'%')}
    ${clusterBar('RAM (GiB)', ram, ramLim, v => v.toFixed(1)+' GiB')}
    ${vramLim > 0 || vram > 0 ? clusterBar('VRAM (GiB)', vram, vramLim, v => v.toFixed(1)+' GiB') : ''}
  </div>`;
}

export async function loadResources() {
  try {
    const [cap, hw] = await Promise.all([
      fetch('/api/capacity').then(r => r.json()).catch(() => null),
      fetch('/api/hardware').then(r => r.json()).catch(() => ({})),
    ]);
    // H1: cache cpu_cores_total for _usageHtml
    if (cap && cap.cpu_cores_total) S.cpuTotalCores = cap.cpu_cores_total;
    renderClusterUsage(cap);  // M1/M2
    renderResourceBars(cap, hw);
    renderHardware(hw);
    const ts = document.getElementById('hw-refresh-ts');
    if (ts) ts.textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    const el = document.getElementById('res-bars');
    if (el) el.innerHTML = `<div class="empty">Error loading resources: ${escHtml(String(e))}</div>`;
  }
}

export async function loadHardware() {
  try {
    const hw = await fetch('/api/hardware').then(r => r.json());
    renderHardware(hw);
    const ts = document.getElementById('hw-refresh-ts');
    if (ts) ts.textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch(e) { /* ignore */ }
}

export function renderResourceBars(cap, hw) {
  const el = document.getElementById('res-bars');
  if (!el) return;
  if (!S.status) { el.innerHTML = `<div class="empty">${i18n.t('resources.noStatus')}</div>`; return; }
  const sys = S.status.system || {};
  const cfg = S.cfg || {};
  const running = S.status.running || [];

  const cpuUsed = running.reduce((s, t) => s + ((t.resources||{}).cpu_cores||1), 0);
  const cpuLimit = cap ? (cap.cpu_cores_limit || 0) : (sys.cpu_total_cores||0) * (cfg.cpu_limit_pct||50)/100;
  const cpuTotal = sys.cpu_total_cores || 1;
  const cpuPct = cpuLimit > 0 ? Math.min(100, cpuUsed / cpuLimit * 100) : 0;

  const ramUsed = sys.ram_used_gib || 0;
  const ramLimit = cap ? (cap.ram_gib_limit || 0) : (sys.ram_total_gib||0) * (cfg.ram_limit_pct||50)/100;
  const ramTotal = sys.ram_total_gib || 0;
  const ramPct = ramLimit > 0 ? Math.min(100, ramUsed / ramLimit * 100) : 0;

  const maxTasks = cfg.max_running_tasks || 0;
  const runCount = running.length;
  const taskPct = maxTasks > 0 ? Math.min(100, runCount / maxTasks * 100) : 0;

  function bar(label, used, limit, total, pct, unitFn) {
    const color = pct > 85 ? 'var(--fail)' : pct > 60 ? 'var(--warn)' : 'var(--ok)';
    const limitStr = limit > 0 ? unitFn(limit) : i18n.t('status.unlimited');
    return `<div class="res-bar-row">
      <span class="res-bar-label">${label}</span>
      <div class="res-bar-track"><div class="res-bar-fill" style="width:${pct.toFixed(1)}%;background:${color}"></div></div>
      <span class="res-bar-value">${unitFn(used)} / ${limitStr}</span>
    </div>`;
  }

  let html = '';
  html += bar(i18n.t('resources.cpuCores'), cpuUsed, cpuLimit, cpuTotal, cpuPct, v => v.toFixed(0));
  html += bar(i18n.t('resources.ramGib'), ramUsed, ramLimit, ramTotal, ramPct, v => v.toFixed(1));
  if (maxTasks > 0) {
    html += bar(i18n.t('resources.tasksRunning'), runCount, maxTasks, maxTasks, taskPct, v => v.toFixed(0));
  } else {
    html += `<div class="res-bar-row"><span class="res-bar-label">${i18n.t('resources.tasksRunning')}</span>
      <div class="res-bar-track"><div class="res-bar-fill" style="width:0%;background:var(--ok)"></div></div>
      <span class="res-bar-value">${runCount} / ${i18n.t('status.unlimited')}</span></div>`;
  }

  // Limit edit inputs with POST to /api/config
  html += `<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px">${i18n.t('resources.editLimits')}</div>
    <div class="res-bar-row" style="flex-wrap:wrap;gap:8px">
      <label style="font-size:12px;color:var(--text2);min-width:130px">${i18n.t('resources.cpuLimitPct')}</label>
      <input type="number" id="res-cpu-pct" value="${cfg.cpu_limit_pct||50}" min="1" max="100"
        style="width:70px;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:5px;font-size:12px">
      <label style="font-size:12px;color:var(--text2);min-width:130px;margin-left:10px">${i18n.t('resources.ramLimitPct')}</label>
      <input type="number" id="res-ram-pct" value="${cfg.ram_limit_pct||80}" min="10" max="100"
        style="width:70px;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:5px;font-size:12px">
      <label style="font-size:12px;color:var(--text2);min-width:130px;margin-left:10px">${i18n.t('resources.maxRunningTasks')}</label>
      <input type="number" id="res-max-tasks" value="${cfg.max_running_tasks||0}" min="0" max="256"
        style="width:70px;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:5px;font-size:12px">
      <button class="action-btn green" style="padding:4px 12px" data-click="${tqAct([['saveResLimits']])}">Save</button>
    </div>
  </div>`;

  el.innerHTML = html;
}

export async function saveResLimits() {
  const cpu = +(document.getElementById('res-cpu-pct') || {value:50}).value;
  const ram = +(document.getElementById('res-ram-pct') || {value:80}).value;
  const maxTasks = +(document.getElementById('res-max-tasks') || {value:0}).value;
  try {
    await _postJson('/api/config', {
      cpu_limit_pct: cpu, ram_limit_pct: ram, max_running_tasks: maxTasks,
    });
  } catch (e) {
    openAlert(String(e.message || e));
    return;
  }
  await refresh();
  loadResources();
}

export function renderHardware(hw) {
  const el = document.getElementById('hw-content');
  if (!el) return;
  if (!hw) { el.innerHTML = `<div class="empty">${i18n.t('resources.noHardwareData')}</div>`; return; }

  function tempClass(t) {
    if (t == null) return '';
    if (t > 85) return 'temp-hot';
    if (t > 70) return 'temp-warn';
    return 'temp-ok';
  }
  function utilColor(pct) {
    if (pct > 90) return 'var(--fail)';
    if (pct > 70) return 'var(--warn)';
    return 'var(--text)';
  }
  function chip(label, val, extra) {
    return `<div class="hw-chip">${extra||''}<div class="hw-chip-label">${label}</div><div class="hw-chip-val">${val}</div></div>`;
  }
  function bar(pct, color) {
    return `<div class="res-bar-track" style="margin-top:4px"><div class="res-bar-fill" style="width:${Math.min(100,pct).toFixed(1)}%;background:${color||'var(--accent)'}"></div></div>`;
  }

  let html = '';

  // ── System ──────────────────────────────────────────────
  if (hw.os || hw.arch) {
    html += `<div style="font-size:11px;color:var(--muted);margin-bottom:10px">${escHtml(hw.os||'')}${hw.arch ? ' · ' + escHtml(hw.arch) : ''}</div>`;
  }

  // ── CPU ──────────────────────────────────────────────────
  html += `<div class="hw-section"><div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:6px">${i18n.t('hardware.cpu')}</div>`;
  if (hw.cpu_model) {
    html += `<div style="font-size:11px;color:var(--text2);margin-bottom:6px;word-break:break-all">${escHtml(hw.cpu_model)}</div>`;
  }
  html += '<div class="hw-row">';
  if (hw.cpu_physical_cores != null) {
    html += chip(i18n.t('hardware.chip.cores'), `${hw.cpu_physical_cores}P / ${hw.cpu_logical_cores}L`);
  }
  if (hw.cpu_freq_mhz != null) {
    const freqMax = hw.cpu_freq_max_mhz ? ` / ${hw.cpu_freq_max_mhz}` : '';
    html += chip(i18n.t('hardware.chip.freq'), `${hw.cpu_freq_mhz}${freqMax} MHz`);
  }
  if (hw.cpu_util_pct != null) {
    const uc = utilColor(hw.cpu_util_pct);
    html += `<div class="hw-chip"><div class="hw-chip-label">${i18n.t('hardware.chip.utilization')}</div>
      <div class="hw-chip-val" style="color:${uc}">${hw.cpu_util_pct.toFixed(1)}%</div>
      ${bar(hw.cpu_util_pct, uc)}</div>`;
  }
  if (hw.cpu_temp_c != null) {
    html += `<div class="hw-chip"><div class="hw-chip-label">${i18n.t('hardware.chip.temp')}</div><div class="hw-chip-val ${tempClass(hw.cpu_temp_c)}">${hw.cpu_temp_c}°C</div></div>`;
  }
  html += '</div>';

  // Per-core heat strip
  if (hw.cpu_per_core_pct && hw.cpu_per_core_pct.length > 0) {
    html += '<div style="margin-top:8px;display:flex;gap:2px;flex-wrap:wrap">';
    for (let i = 0; i < hw.cpu_per_core_pct.length; i++) {
      const p = hw.cpu_per_core_pct[i];
      const bg = p > 90 ? 'var(--fail)' : p > 70 ? 'var(--warn)' : p > 40 ? 'var(--accent)' : 'var(--bg3)';
      html += `<div title="Core ${i}: ${p}%" style="width:18px;height:18px;border-radius:3px;background:${bg};font-size:8px;color:#fff;display:flex;align-items:center;justify-content:center;opacity:0.9">${p>9?Math.round(p):'·'}</div>`;
    }
    html += '</div>';
  }
  html += '</div>';

  // ── RAM ──────────────────────────────────────────────────
  if (hw.ram_total_gib != null) {
    html += `<div class="hw-section"><div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:6px">${i18n.t('hardware.ram')}</div>`;
    html += '<div class="hw-row">';
    const ramColor = hw.ram_pct > 90 ? 'var(--fail)' : hw.ram_pct > 75 ? 'var(--warn)' : 'var(--accent)';
    html += `<div class="hw-chip"><div class="hw-chip-label">${i18n.t('hardware.chip.used')}</div>
      <div class="hw-chip-val" style="color:${ramColor}">${hw.ram_used_gib.toFixed(1)} / ${hw.ram_total_gib.toFixed(1)} GiB</div>
      ${bar(hw.ram_pct, ramColor)}</div>`;
    html += `<div class="hw-chip"><div class="hw-chip-label">${i18n.t('hardware.chip.available')}</div><div class="hw-chip-val">${hw.ram_available_gib.toFixed(1)} GiB</div></div>`;
    if (hw.swap_total_gib > 0) {
      const swColor = hw.swap_pct > 50 ? 'var(--warn)' : 'var(--muted)';
      html += `<div class="hw-chip"><div class="hw-chip-label">${i18n.t('hardware.chip.swap')}</div>
        <div class="hw-chip-val" style="color:${swColor}">${hw.swap_used_gib.toFixed(1)} / ${hw.swap_total_gib.toFixed(1)} GiB</div>
        ${bar(hw.swap_pct, swColor)}</div>`;
    }
    html += '</div></div>';
  }

  // ── GPU(s) ───────────────────────────────────────────────
  if (hw.gpu && hw.gpu.length > 0) {
    for (const g of hw.gpu) {
      const memPct = g.mem_total_gib > 0 ? g.mem_used_gib / g.mem_total_gib * 100 : 0;
      const memColor = memPct > 85 ? 'var(--fail)' : memPct > 60 ? 'var(--warn)' : '#a855f7';
      const loadColor = g.load_pct > 90 ? 'var(--fail)' : g.load_pct > 70 ? 'var(--warn)' : '#2ecc71';
      html += `<div class="hw-section">
        <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:2px">${escHtml(g.name || 'GPU')}</div>`;
      // driver / CUDA / compute capability sub-line
      const meta = [
        g.driver_version ? 'Driver ' + escHtml(g.driver_version) : null,
        g.cuda_version ? 'CUDA ' + escHtml(g.cuda_version) : null,
        g.compute_capability ? 'CC ' + escHtml(g.compute_capability) : null,
        g.pcie_gen ? `PCIe Gen${g.pcie_gen}×${g.pcie_width}` : null,
      ].filter(Boolean).join(' · ');
      if (meta) html += `<div style="font-size:10px;color:var(--muted);margin-bottom:6px">${meta}</div>`;

      html += '<div class="hw-row">';
      // GPU load
      html += `<div class="hw-chip"><div class="hw-chip-label">${i18n.t('hardware.chip.gpuLoad')}</div>
        <div class="hw-chip-val" style="color:${loadColor}">${g.load_pct}%</div>
        ${bar(g.load_pct, loadColor)}</div>`;
      // VRAM
      html += `<div class="hw-chip"><div class="hw-chip-label">${i18n.t('hardware.chip.vram')}</div>
        <div class="hw-chip-val" style="color:${memColor}">${g.mem_used_gib.toFixed(1)} / ${g.mem_total_gib.toFixed(1)} GiB</div>
        ${bar(memPct, memColor)}</div>`;
      if (g.mem_load_pct != null) {
        html += `<div class="hw-chip"><div class="hw-chip-label">${i18n.t('hardware.chip.memBw')}</div>
          <div class="hw-chip-val" style="color:${utilColor(g.mem_load_pct)}">${g.mem_load_pct}%</div>
          ${bar(g.mem_load_pct, utilColor(g.mem_load_pct))}</div>`;
      }
      if (g.temp_c != null) html += `<div class="hw-chip"><div class="hw-chip-label">${i18n.t('hardware.chip.temp')}</div><div class="hw-chip-val ${tempClass(g.temp_c)}">${g.temp_c}°C</div></div>`;
      if (g.fan_speed_pct != null) html += chip(i18n.t('hardware.chip.fan'), g.fan_speed_pct + '%');
      if (g.power_draw_w != null) {
        const pLim = g.power_limit_w ? ` / ${g.power_limit_w}W` : '';
        html += chip(i18n.t('hardware.chip.power'), g.power_draw_w + pLim + 'W');
      }
      html += '</div>';

      // Clocks row
      if (g.clock_graphics_mhz != null || g.clock_mem_mhz != null) {
        html += '<div class="hw-row" style="margin-top:6px">';
        if (g.clock_graphics_mhz != null) {
          const maxG = g.clock_max_graphics_mhz ? ` / ${g.clock_max_graphics_mhz}` : '';
          html += chip(i18n.t('hardware.chip.coreClock'), `${g.clock_graphics_mhz}${maxG} MHz`);
        }
        if (g.clock_mem_mhz != null) {
          const maxM = g.clock_max_mem_mhz ? ` / ${g.clock_max_mem_mhz}` : '';
          html += chip(i18n.t('hardware.chip.memClock'), `${g.clock_mem_mhz}${maxM} MHz`);
        }
        if (g.clock_sm_mhz != null) html += chip(i18n.t('hardware.chip.smClock'), g.clock_sm_mhz + ' MHz');
        html += '</div>';
      }

      if (g.throttle_reasons && g.throttle_reasons.length) {
        html += `<div style="margin-top:6px;font-size:11px;color:var(--warn)">${i18n.t('hardware.throttle', {reasons: escHtml(g.throttle_reasons.join(', '))})}</div>`;
      }
      html += '</div>';
    }
  } else if (hw.gpu_error) {
    html += `<div class="hw-section" style="color:#f59e0b;font-size:12px">${i18n.t('hardware.gpuDriverError', {error: escHtml(hw.gpu_error)})}</div>`;
  } else {
    html += `<div class="hw-section"><div class="empty" style="color:var(--muted)">${i18n.t('hardware.noGpu')}</div></div>`;
  }

  el.innerHTML = html;
}
