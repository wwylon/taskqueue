// Usage tab: GPU/CPU hours chart (canvas), legend toggles and per-project summary.
// Uses app.js globals at call time: tqAct, i18n.
import { S } from '../core/state.js';
import { latest } from '../core/api.js';
import { escHtml } from '../util/format.js';

export function setUsageRange(days) {
  S.usageDays = days;
  ['1d','7d','30d'].forEach(k => {
    const btn = document.getElementById('usage-range-' + k);
    if (btn) btn.style.fontWeight = (parseInt(k) === days ? '700' : '400');
  });
  loadUsage();
}

// W6.17: legend toggle state — key = proj+':gpu' or proj+':cpu'
// W6.17: cached series for hover redraw

export function setUsageType(type) {
  S.usageChartType = type;
  ['line','area','bar'].forEach(t => {
    const btn = document.getElementById('usage-type-' + t);
    if (btn) btn.style.fontWeight = t === type ? '700' : '';
  });
  _drawUsageChart();
}

function _drawUsageChart() {
  if (!S.usageChart) return;
  const {canvas, allSeries, tsSorted, maxY, COLORS} = S.usageChart;
  // ts -> column index (was tsSorted.indexOf per point: quadratic on long ranges)
  const tsPos = new Map(tsSorted.map((t, i) => [t, i]));
  const W = canvas.offsetWidth || 640;
  canvas.width = W;
  const H = canvas.height = 240;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,W,H);

  const pad = {l:56, r:20, t:20, b:40};
  const chartW = W - pad.l - pad.r;
  const chartH = H - pad.t - pad.b;

  // Y-axis title
  ctx.save();
  ctx.translate(12, pad.t + chartH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillStyle = '#6b7280'; ctx.font = '11px sans-serif'; ctx.textAlign = 'center';
  ctx.fillText(i18n.t('usage.hours'), 0, 0);
  ctx.restore();

  // Gridlines + Y labels
  ctx.strokeStyle = 'rgba(100,110,140,0.25)'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + chartH - (i / 4) * chartH;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + chartW, y); ctx.stroke();
    ctx.fillStyle = '#6b7280'; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText((maxY * i / 4).toFixed(2), pad.l - 6, y + 3);
  }

  // Chart area border
  ctx.strokeStyle = 'rgba(100,110,140,0.4)'; ctx.lineWidth = 1;
  ctx.strokeRect(pad.l, pad.t, chartW, chartH);

  // Series rendering (line / area / bar)
  if (S.usageChartType === 'bar') {
    const nSeries = allSeries.length;
    const slotW = tsSorted.length > 1 ? chartW / tsSorted.length : chartW;
    const barW = Math.max(2, slotW / (nSeries + 1));
    allSeries.forEach(([proj, pts], idx) => {
      const color = COLORS[idx % COLORS.length];
      const gpuKey = proj + ':gpu', cpuKey = proj + ':cpu';
      const showGpu = S.usageToggle[gpuKey] !== false;
      const showCpu = S.usageToggle[cpuKey] !== false;
      pts.forEach(p => {
        const ti = tsPos.get(p.ts);
        const cx = pad.l + (ti / Math.max(tsSorted.length - 1, 1)) * chartW - (nSeries - 1) * barW / 2 + idx * barW;
        if (showGpu && p.gpu_hours) {
          const bh = (p.gpu_hours / maxY) * chartH;
          ctx.fillStyle = color; ctx.globalAlpha = 0.85;
          ctx.fillRect(cx - barW / 2, pad.t + chartH - bh, barW * 0.7, bh);
          ctx.globalAlpha = 1;
        }
        if (showCpu && p.cpu_hours) {
          const base = showGpu ? (p.gpu_hours / maxY) * chartH : 0;
          const bh = ((p.cpu_hours || 0) / maxY) * chartH;
          ctx.fillStyle = color; ctx.globalAlpha = 0.4;
          ctx.fillRect(cx - barW / 2, pad.t + chartH - base - bh, barW * 0.7, bh);
          ctx.globalAlpha = 1;
        }
      });
    });
  } else {
    allSeries.forEach(([proj, pts], idx) => {
      const color = COLORS[idx % COLORS.length];
      const gpuKey = proj + ':gpu', cpuKey = proj + ':cpu';
      const showGpu = S.usageToggle[gpuKey] !== false;
      const showCpu = S.usageToggle[cpuKey] !== false;

      if (showGpu) {
        ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.setLineDash([]);
        ctx.beginPath();
        pts.forEach((p, i) => {
          const x = pad.l + (tsPos.get(p.ts) / Math.max(tsSorted.length - 1, 1)) * chartW;
          const y = pad.t + chartH - (p.gpu_hours / maxY) * chartH;
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
        if (S.usageChartType === 'area') {
          const x0 = pad.l + (tsPos.get(pts[0].ts) / Math.max(tsSorted.length-1,1)) * chartW;
          const xN = pad.l + (tsPos.get(pts[pts.length-1].ts) / Math.max(tsSorted.length-1,1)) * chartW;
          ctx.beginPath();
          pts.forEach((p, i) => {
            const x = pad.l + (tsPos.get(p.ts) / Math.max(tsSorted.length-1,1)) * chartW;
            const y = pad.t + chartH - (p.gpu_hours / maxY) * chartH;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          });
          ctx.lineTo(xN, pad.t + chartH); ctx.lineTo(x0, pad.t + chartH); ctx.closePath();
          ctx.globalAlpha = 0.15; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
        }
      }
      if (showCpu) {
        ctx.strokeStyle = color; ctx.setLineDash([4, 3]); ctx.lineWidth = 1.5;
        ctx.beginPath();
        pts.forEach((p, i) => {
          const x = pad.l + (tsPos.get(p.ts) / Math.max(tsSorted.length - 1, 1)) * chartW;
          const y = pad.t + chartH - ((p.cpu_hours || 0) / maxY) * chartH;
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
        if (S.usageChartType === 'area') {
          const x0 = pad.l + (tsPos.get(pts[0].ts) / Math.max(tsSorted.length-1,1)) * chartW;
          const xN = pad.l + (tsPos.get(pts[pts.length-1].ts) / Math.max(tsSorted.length-1,1)) * chartW;
          ctx.beginPath();
          pts.forEach((p, i) => {
            const x = pad.l + (tsPos.get(p.ts) / Math.max(tsSorted.length-1,1)) * chartW;
            const y = pad.t + chartH - ((p.cpu_hours || 0) / maxY) * chartH;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          });
          ctx.lineTo(xN, pad.t + chartH); ctx.lineTo(x0, pad.t + chartH); ctx.closePath();
          ctx.globalAlpha = 0.10; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
        }
        ctx.setLineDash([]);
      }
    });
  }

  // X-axis labels
  ctx.fillStyle = '#6b7280'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
  const labelCount = Math.min(tsSorted.length, 6);
  for (let i = 0; i < labelCount; i++) {
    const tidx = Math.round(i * (tsSorted.length - 1) / Math.max(labelCount - 1, 1));
    const ts = tsSorted[tidx];
    const d = new Date(ts * 1000);
    const label = (d.getMonth()+1) + '/' + d.getDate() + ' ' + d.getHours() + 'h';
    const x = pad.l + (tidx / Math.max(tsSorted.length - 1, 1)) * chartW;
    ctx.fillText(label, x, pad.t + chartH + 16);
  }

  S.usageChart._pad = pad; S.usageChart._chartW = chartW; S.usageChart._chartH = chartH;
}

function _setupUsageHover(canvas) {
  const tt = document.getElementById('usage-tooltip');
  // One tooltip update per animation frame, and none while the pointer stays on the same column.
  let _raf = 0, _lastEv = null, _lastIdx = -1;
  canvas.onmousemove = function(ev) {
    _lastEv = ev;
    if (_raf) return;
    const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (f => setTimeout(f, 16));
    _raf = raf(() => { _raf = 0; _hover(_lastEv); });
  };
  function _hover(e) {
    if (!S.usageChart || !tt) return;
    const {allSeries, tsSorted, maxY, COLORS, _pad, _chartW, _chartH} = S.usageChart;
    if (!_pad) return;
    const rect = canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * (canvas.width / rect.width);
    const relX = mx - _pad.l;
    if (relX < 0 || relX > _chartW) { tt.style.display = 'none'; return; }
    // find nearest timestamp index
    const frac = relX / _chartW;
    const tsIdx = Math.round(frac * (tsSorted.length - 1));
    const ts = tsSorted[tsIdx];
    if (!ts) { tt.style.display = 'none'; _lastIdx = -1; return; }
    if (tsIdx === _lastIdx && tt.style.display === 'block') return;
    _lastIdx = tsIdx;
    const date = new Date(ts * 1000);
    const label = date.toLocaleDateString() + ' ' + date.getHours() + ':00';
    let html = `<strong>${label}</strong>`;
    allSeries.forEach(([proj, pts], idx) => {
      const pt = pts.find(p => p.ts === ts);
      const c = COLORS[idx % COLORS.length];
      if (pt) {
        html += `<br><span style="color:${c}">${escHtml(proj)}</span>: GPU ${pt.gpu_hours.toFixed(3)}h · CPU ${(pt.cpu_hours||0).toFixed(3)}h`;
      }
    });
    tt.innerHTML = html;
    // Position tooltip
    const xRatio = (tsIdx / Math.max(tsSorted.length - 1, 1));
    const ttX = _pad.l + xRatio * _chartW;
    const canvasW = rect.width;
    const ttLeft = ttX / canvas.width * canvasW;
    tt.style.display = 'block';
    tt.style.left = (ttLeft + 8) + 'px';
    tt.style.top = '4px';
    if (parseFloat(tt.style.left) + 180 > canvasW) {
      tt.style.left = (ttLeft - 180) + 'px';
    }

    // Crosshair redraw
    _drawUsageChart();
    const ctx = canvas.getContext('2d');
    const crossX = ttX;
    ctx.strokeStyle = 'rgba(180,190,220,0.5)'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(crossX, _pad.t); ctx.lineTo(crossX, _pad.t + _chartH); ctx.stroke();
    ctx.setLineDash([]);
  };
  canvas.onmouseleave = function() {
    if (tt) tt.style.display = 'none';
    _drawUsageChart();
  };
}

export function loadUsage() {
  const projSel = document.getElementById('usage-project-select');
  const proj = projSel ? projSel.value : '';
  const url = '/api/usage?days=' + S.usageDays + (proj ? '&project=' + encodeURIComponent(proj) : '');
  const live = latest('usage');
  fetch(url).then(r => r.ok ? r.json() : Promise.reject(new Error('Error ' + r.status))).then(data => {
    if (!live()) return;
    const canvas = document.getElementById('usage-canvas');
    const emptyEl = document.getElementById('usage-empty');
    const legendEl = document.getElementById('usage-legend');
    if (!canvas) return;

    // Populate project filter
    if (projSel && data.projects) {
      const cur = projSel.value;
      while (projSel.options.length > 1) projSel.remove(1);
      data.projects.forEach(p => {
        const o = document.createElement('option');
        o.value = p; o.textContent = p;
        if (p === cur) o.selected = true;
        projSel.appendChild(o);
      });
    }

    const byProj = data.by_project || {};
    const allSeries = Object.entries(byProj);
    if (!allSeries.length) {
      canvas.style.display = 'none';
      if (emptyEl) emptyEl.style.display = '';
      if (legendEl) legendEl.innerHTML = '';
      S.usageChart = null;
      return;
    }
    canvas.style.display = 'block';
    if (emptyEl) emptyEl.style.display = 'none';

    const tsSet = new Set();
    allSeries.forEach(([, pts]) => pts.forEach(p => tsSet.add(p.ts)));
    const tsSorted = Array.from(tsSet).sort((a,b) => a-b);

    const COLORS = ['#5b7cf6','#2ecc71','#e74c3c','#f39c12','#a855f7','#06b6d4','#fb923c'];

    let maxY = 0;
    allSeries.forEach(([, pts]) => pts.forEach(p => {
      if (p.gpu_hours > maxY) maxY = p.gpu_hours;
      if (p.cpu_hours > maxY) maxY = p.cpu_hours;
    }));
    if (maxY === 0) maxY = 1;

    S.usageChart = {canvas, allSeries, tsSorted, maxY, COLORS};
    _drawUsageChart();
    _setupUsageHover(canvas);

    // W9.7: summary table per project
    const sumEl = document.getElementById('usage-summary-table');
    if (sumEl && allSeries.length) {
      const rows = allSeries.map(([pname, pts]) => {
        const totalGpu = pts.reduce((a, p) => a + (p.gpu_hours || 0), 0);
        const totalCpu = pts.reduce((a, p) => a + (p.cpu_hours || 0), 0);
        const lastTs = pts.length ? new Date(pts[pts.length-1].ts * 1000).toLocaleString() : '—';
        const gt = (data.gpu_today || {})[pname];
        const todayTxt = gt ? gt.used_hours.toFixed(2) + (gt.limit_hours > 0 ? ' / ' + gt.limit_hours : '') : '—';
        const todayCls = gt && gt.limit_hours > 0 && gt.remaining_hours <= 0 ? ' style="padding:6px 10px;text-align:right;color:var(--red,#e74c3c)"' : ' style="padding:6px 10px;text-align:right"';
        return `<tr>
          <td style="padding:6px 10px">${escHtml(pname)}</td>
          <td style="padding:6px 10px;text-align:right">${totalGpu.toFixed(2)}</td>
          <td style="padding:6px 10px;text-align:right">${totalCpu.toFixed(2)}</td>
          <td${todayCls}>${todayTxt}</td>
          <td style="padding:6px 10px;color:var(--text2)">${lastTs}</td>
        </tr>`;
      }).join('');
      sumEl.innerHTML = `<table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--border)">
          <th style="padding:6px 10px;text-align:left">${i18n.t('usage.thProject')}</th>
          <th style="padding:6px 10px;text-align:right">${i18n.t('usage.thGpuHours')}</th>
          <th style="padding:6px 10px;text-align:right">${i18n.t('usage.thCpuHours')}</th>
          <th style="padding:6px 10px;text-align:right" title="${i18n.t('usage.thGpuTodayHint')}">${i18n.t('usage.thGpuToday')}</th>
          <th style="padding:6px 10px;text-align:left">${i18n.t('usage.thLastData')}</th>
        </tr></thead><tbody>${rows}</tbody></table>`;
    } else if (sumEl) {
      sumEl.innerHTML = `<div class="empty">${i18n.t('usage.noData')}</div>`;
    }

    // W6.17: clickable legend — click gpu/cpu swatch to toggle that line
    if (legendEl) {
      const parts = allSeries.map(([pname], idx) => {
        const c = COLORS[idx % COLORS.length];
        const gpuKey = pname + ':gpu', cpuKey = pname + ':cpu';
        const gpuOff = S.usageToggle[gpuKey] === false;
        const cpuOff = S.usageToggle[cpuKey] === false;
        return `<span style="margin-right:8px;cursor:default">` +
          `<span title="${i18n.t('chart.toggleGpu')}" data-click="${tqAct([['tqToggleUsageSeries', gpuKey]])}" ` +
          `style="display:inline-block;width:14px;height:3px;background:${gpuOff?'#444':c};margin-right:2px;vertical-align:middle;border-radius:1px;cursor:pointer"></span>` +
          `<span title="${i18n.t('chart.toggleCpu')}" data-click="${tqAct([['tqToggleUsageSeries', cpuKey]])}" ` +
          `style="display:inline-block;width:14px;height:0;border-bottom:2px dashed ${cpuOff?'#444':c};margin-right:4px;vertical-align:middle;cursor:pointer"></span>` +
          `<span style="opacity:${(gpuOff&&cpuOff)?0.4:1}">${escHtml(pname)}</span></span>`;
      });
      legendEl.innerHTML = parts.join('') +
        `<span style="color:#6b7280;font-size:11px">&nbsp;${i18n.t('usage.legendHint')}</span>`;
    }
  }).catch(() => {});
}


export function tqToggleUsageSeries(key) {
  S.usageToggle[key] = (S.usageToggle[key] === false ? undefined : false);
  loadUsage();
}

