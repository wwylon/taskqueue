// Generic modal, YAML modal, metrics/compare modal and toasts. Uses app.js globals at call time: i18n,
// _usageHtml, _etaRemainingSec, _drawSparklineOnCanvas.
import { S } from '../core/state.js';
import { escHtml, dur } from '../util/format.js';

// ---- W2.5: Generic modal ----

let _genModalOnDismiss = null;
let _genModalReturnFocus = null;

export function _closeGenModal() {
  const el = document.getElementById('gen-modal');
  if (el) el.classList.remove('open');
  const back = _genModalReturnFocus;
  _genModalReturnFocus = null;
  if (back && typeof back.focus === 'function') { try { back.focus(); } catch (_) {} }
  const dismiss = _genModalOnDismiss;
  _genModalOnDismiss = null;
  if (dismiss) {
    try { dismiss(); } catch (_) {}
  }
}
if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('keydown', e => {
  const el = document.getElementById('gen-modal');
  if (e.key === 'Tab') {
    // Trap Tab inside whichever modal is visible (last in DOM order wins).
    const shown = [...document.querySelectorAll('.modal-bg')].filter(m => getComputedStyle(m).display !== 'none');
    const top = shown[shown.length - 1];
    if (!top) return;
    const f = [...top.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
      .filter(n => !n.disabled && n.offsetParent !== null);
    if (f.length) {
      const i = f.indexOf(document.activeElement);
      const next = e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i === -1 || i === f.length - 1 ? 0 : i + 1);
      f[next].focus();
      e.preventDefault();
    }
    return;
  }
  if (e.key !== 'Escape') return;
  if (el && el.classList.contains('open')) {
    _closeGenModal();
    e.stopImmediatePropagation();
  }
});

/**
 * openModal({title, body, actions: [{label, primary, onClick}], onDismiss})
 * Returns a close function. Async onClick is awaited; confirm stays disabled until done.
 */
export function openModal({ title = '', body = '', actions = [], onDismiss = null } = {}) {
  const bg = document.getElementById('gen-modal');
  // Replacing an open modal counts as dismissing it: its onDismiss must still run.
  if (bg.classList.contains('open') && _genModalOnDismiss) {
    const prev = _genModalOnDismiss;
    _genModalOnDismiss = null;
    try { prev(); } catch (_) {}
  } else if (!bg.classList.contains('open')) {
    _genModalReturnFocus = document.activeElement;
  }
  _genModalOnDismiss = typeof onDismiss === 'function' ? onDismiss : null;
  document.getElementById('gen-modal-title').textContent = title;
  const bodyEl = document.getElementById('gen-modal-body');
  if (typeof body === 'string') bodyEl.innerHTML = body;
  else { bodyEl.innerHTML = ''; bodyEl.appendChild(body); }
  const footer = document.getElementById('gen-modal-footer');
  footer.innerHTML = '';
  actions.forEach(({ label = 'OK', primary = false, onClick = null }) => {
    const btn = document.createElement('button');
    btn.className = primary ? 'btn-primary' : 'btn-cancel';
    btn.textContent = label;
    btn.onclick = async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      // Confirm started or cancel clicked — don't also run onDismiss on close/Escape.
      _genModalOnDismiss = null;
      const orig = btn.textContent;
      try {
        if (onClick) {
          const ret = onClick();
          if (ret && typeof ret.then === 'function') {
            btn.textContent = '…';
            await ret;
          }
        }
      } finally {
        const el = document.getElementById('gen-modal');
        if (el) el.classList.remove('open');
        btn.disabled = false;
        btn.textContent = orig;
      }
    };
    footer.appendChild(btn);
  });
  bg.classList.add('open');
  const first = footer.querySelector('.btn-primary') || footer.querySelector('button');
  if (first && typeof first.focus === 'function') { try { first.focus(); } catch (_) {} }
  return _closeGenModal;
}

export function openAlert(msg, title) {
  if (title === undefined) title = i18n.t('modal.noticeTitle');
  // pre-line keeps the message's line breaks (multi-line alerts such as "Why not running?").
  openModal({ title, body: `<div style="white-space:pre-line">${escHtml(msg)}</div>`, actions: [{ label: i18n.t('btn.ok'), primary: true }] });
}

export function openConfirm(msg, onConfirm, opts) {
  opts = opts || {};
  var title = opts.title !== undefined ? opts.title : i18n.t('modal.confirmTitle');
  var confirmLabel = opts.confirmLabel !== undefined ? opts.confirmLabel : i18n.t('btn.ok');
  var cancelLabel = opts.cancelLabel !== undefined ? opts.cancelLabel : i18n.t('btn.cancel');
  openModal({
    title,
    body: escHtml(msg),
    onDismiss: opts.onCancel || null,
    actions: [
      { label: cancelLabel, onClick: opts.onCancel || null },
      { label: confirmLabel, primary: true, onClick: onConfirm },
    ],
  });
}

// L2: copy YAML to clipboard
// W3.5: YAML modal
let _yamlModalProject = null, _yamlModalTaskId = null, _yamlModalMtimeNs = null;
export async function openYamlModal(project, taskId) {
  _yamlModalProject = project; _yamlModalTaskId = taskId;
  // Reset edit mode
  document.getElementById('yaml-modal-edit').style.display = 'none';
  document.getElementById('yaml-modal-pre').style.display = '';
  document.getElementById('yaml-edit-btn').textContent = 'Edit';
  document.getElementById('yaml-save-btn').style.display = 'none';
  const modal = document.getElementById('yaml-modal');
  const pre = document.getElementById('yaml-modal-pre');
  const err = document.getElementById('yaml-modal-error');
  const title = document.getElementById('yaml-modal-title');
  title.textContent = `YAML — ${project} / ${taskId}`;
  pre.textContent = 'Loading…';
  err.style.display = 'none';
  modal.style.display = 'flex';
  try {
    const r = await fetch(`/api/tasks/${encodeURIComponent(project)}/${encodeURIComponent(taskId)}/yaml`);
    if (!r.ok) {
      const msg = await r.text().catch(() => r.statusText);
      pre.textContent = '';
      err.textContent = `Error ${r.status}: ${msg}`;
      err.style.display = 'block';
      return;
    }
    _yamlModalMtimeNs = r.headers.get('X-File-Mtime-Ns');
    const text = await r.text();
    pre.textContent = text;
  } catch(e) {
    pre.textContent = '';
    err.textContent = 'Fetch failed: ' + e;
    err.style.display = 'block';
  }
}

export function closeYamlModal() {
  document.getElementById('yaml-modal').style.display = 'none';
  _yamlModalProject = null; _yamlModalTaskId = null; _yamlModalMtimeNs = null;
}

// W9.10: toggle edit mode in YAML modal
export function yamlModalToggleEdit() {
  const pre = document.getElementById('yaml-modal-pre');
  const ta = document.getElementById('yaml-modal-edit');
  const btn = document.getElementById('yaml-edit-btn');
  const saveBtn = document.getElementById('yaml-save-btn');
  if (ta.style.display === 'none') {
    ta.value = pre.textContent;
    ta.style.display = 'block';
    pre.style.display = 'none';
    btn.textContent = 'Cancel';
    saveBtn.style.display = '';
  } else {
    ta.style.display = 'none';
    pre.style.display = '';
    btn.textContent = 'Edit';
    saveBtn.style.display = 'none';
  }
}

export async function yamlModalSave() {
  if (!_yamlModalProject || !_yamlModalTaskId) return;
  const ta = document.getElementById('yaml-modal-edit');
  const err = document.getElementById('yaml-modal-error');
  const saveBtn = document.getElementById('yaml-save-btn');
  err.style.display = 'none';
  saveBtn.disabled = true; saveBtn.textContent = i18n.t('btn.saving');
  try {
    const hdrs = {'Content-Type': 'text/plain'};
    if (_yamlModalMtimeNs) hdrs['X-If-Mtime-Ns'] = _yamlModalMtimeNs;
    const r = await fetch(`/api/tasks/${encodeURIComponent(_yamlModalProject)}/${encodeURIComponent(_yamlModalTaskId)}/yaml`, {
      method: 'PUT',
      headers: hdrs,
      body: ta.value,
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      err.textContent = j.error || `Error ${r.status}`;
      err.style.display = 'block';
    } else {
      document.getElementById('yaml-modal-pre').textContent = ta.value;
      // Refresh mtime so subsequent saves use the new stamp
      const r2 = await fetch(`/api/tasks/${encodeURIComponent(_yamlModalProject)}/${encodeURIComponent(_yamlModalTaskId)}/yaml`);
      if (r2.ok) _yamlModalMtimeNs = r2.headers.get('X-File-Mtime-Ns');
      yamlModalToggleEdit();
    }
  } catch(e) {
    err.textContent = 'Save failed: ' + e;
    err.style.display = 'block';
  } finally {
    saveBtn.disabled = false; saveBtn.textContent = i18n.t('btn.save');
  }
}

// W9.4 + W3.3: resource/metrics modal
let _resourceModalRunId = null;
export function getResourceModalRunId() { return _resourceModalRunId; }
export function _resourceModalBodyHtml(t) {
  const isLive = (S.status && S.status.running || []).some(r => r.run_id === (t.run_id || _resourceModalRunId));
  let html = _usageHtml(t, {
    panel: 'auto',
    runId: t.run_id || _resourceModalRunId,
    project: t.project,
    taskId: t.task_id,
    isRunning: isLive,
  });
  const elapsed = t.elapsed_sec;
  const pct = t.progress_pct;
  if (elapsed != null) {
    const etaRem = _etaRemainingSec(t, elapsed, pct);
    const etaPart = etaRem != null ? ` · ETA ${dur(etaRem)}` : '';
    html += `<div style="margin-top:8px;font-size:11px;color:var(--muted)">Elapsed: ${dur(elapsed)}${etaPart}${pct != null ? ` · ${pct.toFixed(0)}%` : ''}</div>`;
  }
  if (isLive) html += `<div style="margin-top:4px;color:var(--muted);font-size:11px">Updates automatically while running.</div>`;
  return html;
}
export function showResourceModal(runId) {
  _resourceModalRunId = runId;
  const all = [
    ...(S.status && S.status.running || []),
    ...(S.status && S.status.recent || []),
  ];
  const t = all.find(r => r.run_id === runId) || {};
  const modal = document.getElementById('resource-modal');
  const title = document.getElementById('resource-modal-title');
  const body = document.getElementById('resource-modal-body');
  title.textContent = `Metrics — ${t.project||''}/${t.task_id||''} run #${runId}`;
  const html = _resourceModalBodyHtml(t);
  body.innerHTML = html;
  body.dataset.lastHtml = html;
  modal.style.display = 'flex';
  // Draw sparkline once; live status refresh patches body + redraws sparingly via timer.
  _drawSparklineOnCanvas(document.getElementById('resource-sparkline'), runId);
  if (_resourcePollTimer) clearInterval(_resourcePollTimer);
  _resourcePollTimer = setInterval(() => {
    if (!document.getElementById('resource-modal') || document.getElementById('resource-modal').style.display === 'none') {
      clearInterval(_resourcePollTimer); return;
    }
    if (document.hidden) return;
    // Sparkline only — body text is patched from _patchDashboardLive / status refresh.
    _drawSparklineOnCanvas(document.getElementById('resource-sparkline'), _resourceModalRunId);
  }, 5000);
}
let _resourcePollTimer = null;

// Run compare: first click picks run A, second click on another run shows the diff in the metrics modal.
let _compareRunA = null;
export async function compareRun(runId) {
  if (_compareRunA === null || _compareRunA === runId) {
    _compareRunA = runId;
    showToast(i18n.t('toast.comparePick', {id: runId}), 'info');
    return;
  }
  const a = _compareRunA;
  _compareRunA = null;
  try {
    const r = await fetch(`/api/runs/diff?a=${a}&b=${runId}`);
    const d = await r.json();
    if (!r.ok) { showToast(d.error || 'compare failed', 'err'); return; }
    document.getElementById('resource-modal-title').textContent = i18n.t('compare.title', {a: a, b: runId});
    const fmt = v => escHtml(typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? '-'));
    const rows = [];
    Object.keys(d.changes).forEach(k => {
      const c = d.changes[k];
      if (k === 'metrics') {
        Object.keys(c).forEach(m => rows.push([`metrics.${m}`, c[m].a, c[m].b]));
      } else if (k === 'env_keys') {
        rows.push([k, `${i18n.t('compare.only')} A: ${c.only_a.join(', ') || '-'}`, `${i18n.t('compare.only')} B: ${c.only_b.join(', ') || '-'}`]);
      } else {
        rows.push([k, c.a, c.b]);
      }
    });
    const body = document.getElementById('resource-modal-body');
    body.innerHTML = rows.length
      ? `<table class="data-table"><thead><tr><th></th><th>#${a}</th><th>#${runId}</th></tr></thead><tbody>${
          rows.map(x => `<tr><td>${escHtml(x[0])}</td><td>${fmt(x[1])}</td><td>${fmt(x[2])}</td></tr>`).join('')
        }</tbody></table>`
      : `<p>${i18n.t('compare.identical')}</p>`;
    delete body.dataset.lastHtml;
    _resourceModalRunId = null;
    const sc = document.getElementById('resource-sparkline');
    if (sc) sc.style.display = 'none';
    document.getElementById('resource-modal').style.display = 'flex';
  } catch (e) {
    showToast(String(e), 'err');
  }
}
export function closeResourceModal() {
  document.getElementById('resource-modal').style.display = 'none';
  if (_resourcePollTimer) { clearInterval(_resourcePollTimer); _resourcePollTimer = null; }
  const sc = document.getElementById('resource-sparkline');
  if (sc) sc.style.display = 'none';
  const body = document.getElementById('resource-modal-body');
  if (body) delete body.dataset.lastHtml;
  _resourceModalRunId = null;
}

export async function yamlModalCopy() {
  const pre = document.getElementById('yaml-modal-pre');
  const btn = document.getElementById('yaml-copy-btn');
  try {
    await navigator.clipboard.writeText(pre.textContent);
    const orig = btn.textContent;
    btn.textContent = i18n.t('btn.copied');
    setTimeout(() => { btn.textContent = orig; }, 1500);
  } catch(e) {
    openAlert('Copy failed: ' + e, i18n.t('modal.copyFailedTitle'));
  }
}

/** Non-blocking action feedback (success / error / info). */
export function showToast(message, kind, ms, onClick) {
  kind = kind || 'info';
  ms = ms == null ? 3500 : ms;
  let host = document.getElementById('tq-toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'tq-toast-host';
    host.setAttribute('aria-live', 'polite');
    document.body.appendChild(host);
  }
  while (host.children.length >= 3) host.removeChild(host.firstChild);
  const el = document.createElement('div');
  el.className = 'tq-toast tq-toast-' + kind;
  el.textContent = message;
  if (String(message).includes('\n')) el.style.whiteSpace = 'pre-line';
  el.onclick = () => { el.remove(); if (onClick) onClick(); };
  if (onClick) el.style.cursor = 'pointer';
  host.appendChild(el);
  setTimeout(() => { if (el.parentNode) el.remove(); }, ms);
}
/** Toast with an Undo button. `onUndo` runs if it is clicked before `ms` elapse. */
export function showUndoToast(message, onUndo, ms) {
  ms = ms == null ? 5000 : ms;
  let host = document.getElementById('tq-toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'tq-toast-host';
    host.setAttribute('aria-live', 'polite');
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = 'tq-toast tq-toast-info';
  const txt = document.createElement('span');
  txt.textContent = message + ' ';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'cfg-btn';
  btn.textContent = i18n.t('btn.undo');
  btn.onclick = ev => { if (ev && ev.stopPropagation) ev.stopPropagation(); el.remove(); onUndo(); };
  el.appendChild(txt);
  el.appendChild(btn);
  host.appendChild(el);
  setTimeout(() => { if (el.parentNode) el.remove(); }, ms);
  return el;
}

/** Toast anchored near a control (e.g. copy-name button). Falls back to showToast. */
export function showAnchorToast(anchor, message, kind, ms) {
  if (!anchor) {
    showToast(message, kind, ms);
    return;
  }
  kind = kind || 'ok';
  ms = ms == null ? 2000 : ms;
  if (anchor._anchorToastTimer) clearTimeout(anchor._anchorToastTimer);
  if (anchor._anchorToastEl) anchor._anchorToastEl.remove();
  const el = document.createElement('div');
  el.className = 'tq-anchor-toast tq-toast tq-toast-' + kind;
  el.textContent = message;
  el.setAttribute('role', 'status');
  document.body.appendChild(el);
  const place = () => {
    const r = anchor.getBoundingClientRect();
    const tr = el.getBoundingClientRect();
    const margin = 6;
    let top = r.top - tr.height - margin;
    let left = r.left + (r.width - tr.width) / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tr.width - 8));
    if (top < 8) top = r.bottom + margin;
    el.style.top = top + 'px';
    el.style.left = left + 'px';
  };
  place();
  anchor._anchorToastEl = el;
  anchor._anchorToastTimer = setTimeout(() => {
    if (el.parentNode) el.remove();
    if (anchor._anchorToastEl === el) {
      anchor._anchorToastEl = null;
      anchor._anchorToastTimer = null;
    }
  }, ms);
  el.onclick = () => {
    if (el.parentNode) el.remove();
    if (anchor._anchorToastTimer) clearTimeout(anchor._anchorToastTimer);
    anchor._anchorToastEl = null;
    anchor._anchorToastTimer = null;
  };
}
