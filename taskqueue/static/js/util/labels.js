// Status badge, wait/error labels, resource tags, copy-name button and sort-header marks (small HTML helpers).
// Uses app.js globals at call time (i18n, tqAct, TQ_THIS).
import { escHtml } from './format.js';

export function badge(status, lastErrorCode, blockReason) {
  if (status === 'completed' && lastErrorCode === 'tq.run.negative') {
    // Expected "no" result (negative_exit_codes): completed, but visibly not a plain success.
    const t = globalThis.i18n ? globalThis.i18n.t('status.negativeTitle') : '';
    return `<span class="badge badge-completed badge-negative" title="${escHtml(t)}">${escHtml(globalThis.i18n ? globalThis.i18n.t('status.negative') : 'negative')}</span>`;
  }
  const waiting = status === 'queued' && lastErrorCode && String(lastErrorCode).startsWith('tq.block.');
  const label = waiting ? 'waiting' : status;
  let cls = waiting ? 'badge-waiting' : `badge-${String(status).replace(/[^A-Za-z0-9_-]/g, '')}`;
  if (waiting) {
    const kind = _waitKind(lastErrorCode, blockReason);
    if (kind === 'ok') cls += ' badge-waiting-ok';
    else if (kind === 'bad') cls += ' badge-waiting-bad';
    else cls += ' badge-waiting-attn';
  }
  return `<span class="badge ${cls}">${escHtml(label)}</span>`;
}

/** Prefer human block_reason; never show a bare tq.* code as primary text. */
const _ERROR_LABELS = {
  'tq.infra.clock_jump': 'Host slept/woke — re-queued',
  'tq.infra.worker_lost': 'Worker lost — lease expired',
  'tq.infra.lease_expired_worker_alive': 'Lease expired (worker still online)',
  'tq.infra.hung': 'Hung task — re-queued',
  'tq.stop.ram': 'Stopped — host RAM hard limit',
  'tq.dep.cancelled': 'Cancelled — dependency failed',
  'tq.dep.negative': 'Cancelled — dependency result was negative',
  'tq.run.negative': 'Completed — negative result',
  'tq.stop.scavenger': 'Suspended — preempted for other work',
  'tq.block.scavenger': 'Waiting — capacity contended',
  'tq.block.deps': 'Waiting on dependencies',
  'tq.block.resources': 'Waiting on resources',
  'tq.block.exclusive': 'Waiting on exclusive gate',
  'tq.block.exclusive_project': 'Waiting on project exclusive gate',
  'tq.block.frozen': 'Project blocked (frozen)',
  'tq.block.paused': 'Project paused',
  'tq.block.mutex': 'Waiting on named lock',
  'tq.run.frozen': 'No output / idle process',
  'tq.run.cap': 'Hit wall-clock cap',
  'tq.run.nonzero': 'Non-zero exit',
  'tq.run.preflight': 'Preflight failed',
};
const _WAIT_OK_CODES = new Set([
  'tq.block.resources', 'tq.block.exclusive', 'tq.block.exclusive_project', 'tq.block.mutex',
]);
const _WAIT_ATTN_CODES = new Set([
  'tq.block.frozen', 'tq.block.paused', 'tq.block.service_down', 'tq.block.service_vram',
  'tq.block.requires_paths', 'tq.block.vram_exceeds_system',
]);
export function _waitKind(code, reason) {
  const r = String(reason || '');
  const c = String(code || '');
  if (
    /dead dependency/i.test(r)
    || /\(not found\)/i.test(r)
    || /\[(failed|interrupted|stopped|disabled)\]/i.test(r)
  ) {
    return 'bad';
  }
  if (_WAIT_ATTN_CODES.has(c)) return 'attn';
  if (_WAIT_OK_CODES.has(c)) return 'ok';
  if (c === 'tq.block.deps') {
    if (/waiting on/i.test(r)) return 'ok';
    return 'attn';
  }
  if (c.startsWith('tq.block.')) return 'attn';
  return 'attn';
}
export function friendlyErrorText(code, reason) {
  const r = (reason || '').trim();
  if (r && !r.startsWith('tq.')) return r;
  if (code && _ERROR_LABELS[code]) return _ERROR_LABELS[code];
  if (r) return r;
  return code || '';
}

export function waitReasonHtml(t) {
  const code = t.last_error_code || '';
  const reason = t.block_reason || '';
  // Show context for queued/stopped/interrupted — not only soft waits
  if (!['queued', 'stopped', 'interrupted', 'failed', 'suspended', 'cancelled'].includes(t.status)) return '';
  if (!code && !reason) {
    if (t.status === 'suspended') {
      return `<div class="wait-reason wait-ok" title="Task checkpointed and exited cleanly. Use Restart to resume.">${i18n.t('task.suspendedCooperative')}</div>`;
    }
    return '';
  }
  const text = friendlyErrorText(code, reason);
  if (!text) return '';
  const tip = [code, reason].filter(Boolean).join(' — ');
  const kind = _waitKind(code, reason);
  const kindCls = kind === 'ok' ? 'wait-ok' : (kind === 'bad' ? 'wait-bad' : 'wait-attn');
  return `<div class="wait-reason ${kindCls}" title="${escHtml(tip)}">${escHtml(text)}</div>`;
}
export function res(resources, peakRam = null, peakVram = null) {
  const parts = [];
  if (resources) {
    if (resources.cpu_cores) parts.push(`${resources.cpu_cores}cpu`);
    if (resources.gpu_vram_gib) parts.push(`${resources.gpu_vram_gib}G gpu`);
    if (resources.ram_gib) parts.push(`${resources.ram_gib}G ram`);
  }
  const hasPeaks = peakRam !== null || peakVram;
  let resHtml = parts.length ? `<span class="res-tag">${parts.join(' ')}</span>` : (hasPeaks ? '' : '—');
  if (hasPeaks) {
    const peaks = [];
    if (peakRam !== null) peaks.push(`${peakRam.toFixed(1)}G ram`);
    if (peakVram) peaks.push(`${peakVram.toFixed(1)}G vram`);
    resHtml += `<div style="font-size:10px;color:var(--muted);margin-top:2px;">peak: ${peaks.join(' / ')}</div>`;
  }
  return resHtml || '—';
}

export function _taskCopyIconBtn(project, taskId) {
  const title = escHtml(i18n.t('task.copyNameTitle'));
  return `<button type="button" class="task-copy-btn" title="${title}" aria-label="${title}" data-click="${tqAct([['copyTaskName', String(project), String(taskId), TQ_THIS]])}" data-click-stop><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M4 1.5h7A1.5 1.5 0 0 1 12.5 3V4h.5A1.5 1.5 0 0 1 14 5.5v7A1.5 1.5 0 0 1 12.5 14h-7A1.5 1.5 0 0 1 4 12.5V12H3.5A1.5 1.5 0 0 1 2 10.5v-7A1.5 1.5 0 0 1 3.5 2H4v-.5zm1 1.5v8h7v-8H5zm-2 2v7h.5v-7H3z"/></svg></button>`;
}

// ---- Sort helpers ----

export function _updateSortTh(prefix, field, dir) {
  ['priority','yaml_num','status','run_id','started_at','finished_at','duration','enqueued_at','task_id','project','description'].forEach(f => {
    const el = document.getElementById(`${prefix}th-${f}`);
    if (!el) return;
    el.classList.remove('sort-asc','sort-desc');
    if (f === field) el.classList.add(dir > 0 ? 'sort-asc' : 'sort-desc');
  });
}
