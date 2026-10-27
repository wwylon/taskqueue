// Dashboard gauge sparklines (rolling in-memory history persisted to localStorage) and the run-metrics canvas chart.
// Uses app.js globals at call time: _computeGaugeModel. History state is module-local.
import { S } from '../core/state.js';

const _sparkHist = { running: [], queued: [], cpu: [], gpu: [], ram: [], daemon: [] };
const _sparkColor = { running: '#9184d9', queued: '#fbbf24', cpu: '#9184d9', gpu: '#c4b5fd', ram: '#fbbf24', daemon: '#93c5fd' };
const _SPARK_CAP = 60;
const _SPARK_H = 48;
const _SPARK_KEEP_MS = 30 * 60 * 1000;
let _sparkLastAt = 0;
let _sparkLoaded = false;

export function _sparkEnsureLoaded() {
  if (_sparkLoaded) return;
  _sparkLoaded = true;
  try {
    const raw = sessionStorage.getItem('tq_spark_hist');
    if (!raw) return;
    const data = JSON.parse(raw);
    const now = Date.now();
    Object.keys(_sparkHist).forEach(key => {
      const arr = data[key];
      if (!Array.isArray(arr)) return;
      _sparkHist[key] = arr.filter(s => s && typeof s.v === 'number' && typeof s.t === 'number' && now - s.t < _SPARK_KEEP_MS).slice(-_SPARK_CAP);
    });
  } catch (_) {}
}
// Persisting is for reloads only: write at most every 10 s and when the page is hidden/closed.
let _sparkSavedAt = 0;
export function _sparkSave(force) {
  const now = Date.now();
  if (!force && now - _sparkSavedAt < 10000) return;
  _sparkSavedAt = now;
  try { sessionStorage.setItem('tq_spark_hist', JSON.stringify(_sparkHist)); } catch (_) {}
}
if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener('pagehide', () => _sparkSave(true));
}
if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('visibilitychange', () => { if (document.hidden) _sparkSave(true); });
}
export function _sparkScale(values) {
  const nums = values.map(s => s.v);
  let lo = Math.min.apply(null, nums);
  let hi = Math.max.apply(null, nums);
  const span = hi - lo;
  const mag = Math.max(Math.abs(hi), Math.abs(lo), 1);
  const floor = mag * 0.04;
  if (span < floor) {
    const mid = (lo + hi) / 2;
    lo = mid - floor / 2;
    hi = mid + floor / 2;
  } else {
    const pad = span * 0.15;
    lo -= pad;
    hi += pad;
  }
  if (!(hi > lo)) hi = lo + 1;
  return { lo, hi };
}
export function _sparkUnit(key) {
  if (key === 'ram' || key === 'gpu') return ' GiB';
  if (key === 'daemon') return ' MB';
  return '';
}
export function _sparkFmt(key, v) {
  if (key === 'ram' || key === 'gpu') return v.toFixed(1);
  return String(Math.round(v));
}
export function _sparkCaption(key) {
  const arr = _sparkHist[key] || [];
  if (!arr.length) return '';
  const unit = _sparkUnit(key);
  const last = arr[arr.length - 1].v;
  if (arr.length < 2) return _sparkFmt(key, last) + unit;
  const first = arr[0].v;
  const mins = Math.max(1, Math.round((arr[arr.length - 1].t - arr[0].t) / 60000));
  if (Math.abs(last - first) < 0.05 && key !== 'running' && key !== 'queued' && key !== 'cpu' && key !== 'daemon') {
    return _sparkFmt(key, last) + unit + ' · ' + mins + 'm';
  }
  return _sparkFmt(key, first) + ' → ' + _sparkFmt(key, last) + unit + ' · ' + mins + 'm';
}

export function tqRecordSparks() {
  if (!S.status) return;
  _sparkEnsureLoaded();
  const now = Date.now();
  const g = _computeGaugeModel();
  if (now - _sparkLastAt >= 10000 || !_sparkHist.cpu.length) {
    _sparkLastAt = now;
    const sample = {
      running: ((S.status.running) || []).length,
      queued: S.status.queued_count || 0,
      cpu: g.cpuUsed,
      gpu: g.gpuUsed,
      ram: g.ramUsed,
      daemon: S.status.daemon_rss_mb || 0,
    };
    Object.keys(sample).forEach(key => {
      const arr = _sparkHist[key];
      arr.push({ t: now, v: sample[key] });
      if (arr.length > _SPARK_CAP) arr.shift();
    });
    _sparkSave();
  }
  tqDrawSparks();
}

export function _sparkPoints(values, scale) {
  const w = 100, h = _SPARK_H;
  const top = 3, bot = h - 3;
  const span = scale.hi - scale.lo;
  return values.map((s, i) => {
    const x = values.length === 1 ? w / 2 : (i / (values.length - 1)) * (w - 2) + 1;
    const y = bot - ((s.v - scale.lo) / span) * (bot - top);
    return { x, y };
  });
}

export function _sparkMarkup(key) {
  const values = _sparkHist[key] || [];
  if (!values.length) return '';
  const scale = _sparkScale(values);
  const pts = _sparkPoints(values, scale);
  const line = pts.map(p => p.x.toFixed(1) + ',' + p.y.toFixed(1)).join(' ');
  const color = _sparkColor[key];
  const base = _SPARK_H - 1;
  let poly = line;
  if (pts.length === 1) {
    poly = `1,${pts[0].y.toFixed(1)} 99,${pts[0].y.toFixed(1)}`;
  }
  const area = pts.length === 1
    ? `1,${base} 1,${pts[0].y.toFixed(1)} 99,${pts[0].y.toFixed(1)} 99,${base}`
    : `1,${base} ${line} 99,${base}`;
  return `<polygon points="${area}" fill="${color}" fill-opacity="0.18"/>` +
    `<polyline fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" points="${poly}"/>`;
}

export function tqDrawSparks() {
  document.querySelectorAll('svg[data-spark]').forEach(el => {
    const keys = (el.getAttribute('data-spark') || '').split(/\s+/).filter(Boolean);
    el.innerHTML = keys.map(_sparkMarkup).join('');
  });
  document.querySelectorAll('[data-spark-cap]').forEach(el => {
    const keys = (el.getAttribute('data-spark-cap') || '').split(/\s+/).filter(Boolean);
    el.textContent = keys.map(_sparkCaption).filter(Boolean).join(' · ');
  });
}

export function _paintSparkline(canvas, samples) {
  const PAD_L = 48, PAD_R = 8, PAD_T = 14, PAD_B = 28;
  // Match CSS box so the chart scales with the (larger / mobile) metrics modal.
  const parentW = canvas.parentElement ? canvas.parentElement.clientWidth : 0;
  const cssW = Math.max(1, Math.floor(canvas.clientWidth || canvas.offsetWidth || Math.max(0, parentW - 48) || 860));
  const cssH = Math.max(1, Math.floor(canvas.clientHeight || canvas.offsetHeight || 220));
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = cssW, H = cssH;
  ctx.clearRect(0, 0, W, H);
  const CW = W - PAD_L - PAD_R, CH = H - PAD_T - PAD_B;
  const t0 = samples[0].ts, t1 = samples[samples.length - 1].ts;
  const tSpan = Math.max(t1 - t0, 1);
  const xOf = ts => PAD_L + (ts - t0) / tSpan * CW;
  const yOf = (val, maxVal) => PAD_T + CH - (val / maxVal) * CH;
  // grid lines
  const gridColor = getComputedStyle(document.documentElement).getPropertyValue('--chart-grid').trim()
    || 'rgba(255,255,255,0.06)';
  ctx.strokeStyle = gridColor; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = PAD_T + (CH / 4) * i;
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
  }
  function drawSeries(key, maxVal, color) {
    const pts = samples.filter(s => s[key] != null);
    if (!pts.length) return null;
    ctx.beginPath();
    ctx.fillStyle = color + '28';
    ctx.moveTo(xOf(pts[0].ts), yOf(0, maxVal));
    pts.forEach(s => ctx.lineTo(xOf(s.ts), yOf(s[key], maxVal)));
    ctx.lineTo(xOf(pts[pts.length-1].ts), yOf(0, maxVal));
    ctx.closePath(); ctx.fill();
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 1.5;
    pts.forEach((s, i) => i === 0 ? ctx.moveTo(xOf(s.ts), yOf(s[key], maxVal)) : ctx.lineTo(xOf(s.ts), yOf(s[key], maxVal)));
    ctx.stroke();
    const last = pts[pts.length - 1];
    const lx = xOf(last.ts), ly = yOf(last[key], maxVal);
    ctx.beginPath(); ctx.arc(lx, ly, 3, 0, Math.PI*2); ctx.fillStyle = color; ctx.fill();
    return { lastVal: last[key], lx, ly, maxVal };
  }

  const maxCpu = 100;
  const maxRam = Math.max(...samples.map(s => s.ram_gib || 0)) * 1.1 || 1;
  const maxVram = Math.max(...samples.map(s => s.vram_gib || 0)) * 1.1 || 1;
  const hasVram = samples.some(s => (s.vram_gib || 0) > 0);
  const hasGpuUtil = samples.some(s => s.gpu_util_pct != null);
  const cpuInfo  = drawSeries('cpu_pct', maxCpu, '#60a5fa');
  const ramInfo  = drawSeries('ram_gib', maxRam, '#34d399');
  const vramInfo = hasVram ? drawSeries('vram_gib', maxVram, '#a855f7') : null;
  const gpuInfo  = hasGpuUtil ? drawSeries('gpu_util_pct', 100, '#f472b6') : null;
  const axisMuted = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim() || 'rgba(255,255,255,0.4)';
  ctx.font = '10px monospace'; ctx.textAlign = 'right'; ctx.fillStyle = axisMuted;
  for (let i = 0; i <= 4; i++) {
    ctx.fillText(((1 - i/4) * 100).toFixed(0) + '%', PAD_L - 4, PAD_T + (CH / 4) * i + 4);
  }
  ctx.textAlign = 'left'; ctx.font = '11px monospace';
  let lx = PAD_L;
  if (cpuInfo)  { ctx.fillStyle = '#60a5fa'; ctx.fillText(`CPU ${cpuInfo.lastVal.toFixed(0)}%`, lx, H - 8); lx += 90; }
  if (ramInfo)  { ctx.fillStyle = '#34d399'; ctx.fillText(`RAM ${ramInfo.lastVal.toFixed(2)} GiB / ${maxRam.toFixed(1)}`, lx, H - 8); lx += 160; }
  if (vramInfo) { ctx.fillStyle = '#a855f7'; ctx.fillText(`VRAM ${vramInfo.lastVal.toFixed(2)} GiB / ${maxVram.toFixed(1)}`, lx, H - 8); lx += 170; }
  if (gpuInfo)  { ctx.fillStyle = '#f472b6'; ctx.fillText(`GPU ${gpuInfo.lastVal.toFixed(0)}%`, lx, H - 8); }
}

export async function _drawSparklineOnCanvas(canvas, runId) {
  if (!canvas) return;
  let samples;
  try {
    const r = await fetch(`/api/runs/${runId}/history`);
    if (!r.ok) return;
    const data = await r.json();
    samples = Array.isArray(data) ? data : (data.samples || []);
  } catch(e) { return; }
  // Do not hide the canvas before paint — that flashed the metrics modal every poll.
  if (!samples || samples.length < 2) {
    canvas.style.display = 'none';
    return;
  }
  canvas.style.display = 'block';
  // Layout needs a frame after display:block so clientWidth is real on first open.
  requestAnimationFrame(() => _paintSparkline(canvas, samples));
}

export async function _drawSparkline(runId) {
  // kept for backward compat — log row no longer has a canvas, this is now a no-op
}
