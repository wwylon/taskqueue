// @ts-check
// Fetch auth wrapper, SSE helper and JSON fetch helpers.

// Every fetch carries the stored bearer token. Installed once at import time.
(function installAuthFetch() {
  if (window.__tqAuthFetch) return;
  window.__tqAuthFetch = true;
  // A ?tq_token= link is stored once, then removed from the address bar (history, Referer, screenshots).
  try {
    const sp = new URLSearchParams(location.search);
    const urlTok = sp.get('tq_token');
    if (urlTok) {
      localStorage.setItem('tq_token', urlTok);
      sp.delete('tq_token');
      const qs = sp.toString();
      history.replaceState(history.state, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
    }
  } catch (_) {}
  const _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    const headers = new Headers(init.headers || {});
    const t = localStorage.getItem('tq_token');
    if (t && !headers.has('Authorization')) {
      headers.set('Authorization', 'Bearer ' + t);
    }
    // R8: self-reported operator for audit + mine-scope (same as CLI TQ_OPERATOR / X-TQ-Operator).
    try {
      const op = (localStorage.getItem('tq_operator') || '').trim();
      if (op && !headers.has('X-TQ-Operator')) headers.set('X-TQ-Operator', op);
    } catch (_) {}
    init.headers = headers;
    return _fetch(input, init);
  };
})();

/**
 * EventSource cannot set headers. Fetch a short-lived ticket (POST /api/sse-ticket) and open the stream
 * with ?tq_ticket= so the long-lived token never appears in a URL. Returns an EventSource-like handle
 * (onopen/onmessage/onerror/close/readyState); reports an error (no token-in-URL fallback) if the ticket call fails.
 */
/**
 * F-207: mint a fresh SSE ticket and open EventSource. Reconnects remint the
 * ticket (native EventSource retry reuses a single-use URL → 401).
 */
export function openStream(path, handlers = {}) {
  const h = {
    readyState: 0, onopen: handlers.onopen || null, onmessage: handlers.onmessage || null,
    onerror: handlers.onerror || null, _es: null, _closed: false, _ls: [], _path: path,
    _backoff: 500, _reconnectTimer: 0,
    addEventListener(n, f) { this._ls.push([n, f]); if (this._es) this._es.addEventListener(n, f); },
    close() {
      this._closed = true; this.readyState = 2;
      if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
      if (this._es) this._es.close();
    },
  };
  const connect = async () => {
    if (h._closed) return;
    const tok = localStorage.getItem('tq_token');
    let qs = '';
    if (tok) {
      try {
        const ctl = typeof AbortController === 'function' ? new AbortController() : null;
        const timer = ctl ? setTimeout(() => ctl.abort(), 5000) : 0;
        let r;
        try { r = await fetch('/api/sse-ticket', { method: 'POST', signal: ctl ? ctl.signal : undefined }); }
        finally { if (timer) clearTimeout(timer); }
        const j = r.ok ? await r.json() : null;
        if (!j || !j.ticket) throw new Error('no ticket');
        qs = (path.includes('?') ? '&' : '?') + 'tq_ticket=' + encodeURIComponent(j.ticket);
      } catch (e) {
        h.readyState = 2;
        if (h.onerror && !h._closed) h.onerror(new Event('error'));
        if (!h._closed) {
          h._reconnectTimer = setTimeout(connect, h._backoff);
          h._backoff = Math.min(h._backoff * 2, 15000);
        }
        return;
      }
    }
    if (h._closed) return;
    if (h._es) { try { h._es.close(); } catch (_) {} }
    const es = new EventSource(path + qs);
    h._es = es;
    for (const [n, f] of h._ls) es.addEventListener(n, f);
    es.onopen = ev => {
      h.readyState = es.readyState;
      h._backoff = 500;
      if (h.onopen) h.onopen(ev);
    };
    es.onmessage = ev => { if (h.onmessage) h.onmessage(ev); };
    es.onerror = ev => {
      h.readyState = es.readyState;
      if (h.onerror) h.onerror(ev);
      // CLOSED: remint ticket and reconnect (native retry would reuse spent ticket).
      // Ignore errors from a superseded EventSource after a newer connect() replaced h._es.
      if (!h._closed && h._es === es && es.readyState === 2) {
        try { es.close(); } catch (_) {}
        if (h._reconnectTimer) clearTimeout(h._reconnectTimer);
        h._reconnectTimer = setTimeout(connect, h._backoff);
        h._backoff = Math.min(h._backoff * 2, 15000);
      }
    };
  };
  connect();
  return h;
}

/** @deprecated use openStream — kept as alias for existing call sites */
export function tqEventSource(path) {
  return openStream(path);
}

const _latest = {};
/**
 * Stale-response guard: `const live = latest('key')` bumps the counter for `key`; after each await,
 * `if (!live()) return;` drops the result when a newer call with the same key has started.
 */
export function latest(key) {
  const n = (_latest[key] = (_latest[key] || 0) + 1);
  return () => _latest[key] === n;
}

export async function fetchJson(url, timeoutMs, soft) {
  const ac = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ac ? setTimeout(() => ac.abort(), timeoutMs) : null;
  const opts = ac ? { signal: ac.signal } : {};
  try {
    const r = await fetch(url, opts);
    if (timer) clearTimeout(timer);
    // `soft` is kept for callers; failures always yield null so loaders can
    // keep the last good rows instead of rendering an empty state.
    if (!r.ok) return null;
    return await r.json().catch(() => null);
  } catch (e) {
    if (timer) clearTimeout(timer);
    return null;
  }
}

/** Fetch JSON with ETag/304 support. Returns {data, etag, notModified, wallMs, serverTiming}. */
export async function fetchJsonEtag(url, timeoutMs, etag) {
  const ac = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ac ? setTimeout(() => ac.abort(), timeoutMs) : null;
  const headers = etag ? { 'If-None-Match': `"${etag}"` } : {};
  const opts = { headers, ...(ac ? { signal: ac.signal } : {}) };
  const t0 = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
  try {
    const r = await fetch(url, opts);
    if (timer) clearTimeout(timer);
    const wallMs = Math.round(((typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now()) - t0);
    const hdr = r.headers && typeof r.headers.get === 'function' ? r.headers : null;
    const serverTiming = hdr ? (hdr.get('Server-Timing') || hdr.get('X-TQ-Timing') || '') : '';
    if (r.status === 304) return { data: null, etag, notModified: true, wallMs, serverTiming, status: 304 };
    if (!r.ok) return { data: null, etag, notModified: false, wallMs, serverTiming, status: r.status };
    const data = await r.json().catch(() => null);
    const newEtag = (data && data.etag) || (hdr && hdr.get('ETag')) || etag;
    return { data, etag: newEtag ? newEtag.replace(/"/g, '') : etag, notModified: false, wallMs, serverTiming, status: r.status };
  } catch (e) {
    if (timer) clearTimeout(timer);
    const wallMs = Math.round(((typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now()) - t0);
    return { data: null, etag, notModified: false, wallMs, serverTiming: '', status: 0 };
  }
}

/** True when ?tq_perf=1 or localStorage.tq_perf=1 (queue/dashboard latency dogfood). */
export function tqPerfEnabled() {
  try {
    if (localStorage.getItem('tq_perf') === '1') return true;
  } catch (_) {}
  try {
    return new URLSearchParams(location.search).get('tq_perf') === '1';
  } catch (_) {
    return false;
  }
}

/** Slowest Server-Timing phase name from a header value like "a;dur=12, b;dur=80". */
export function tqDominantTimingPhase(serverTiming) {
  if (!serverTiming) return '';
  let best = '', bestDur = -1;
  for (const part of String(serverTiming).split(',')) {
    const m = part.trim().match(/^([A-Za-z0-9_-]+).*?dur=([0-9.]+)/);
    if (!m) continue;
    const dur = parseFloat(m[2]);
    if (dur > bestDur) { bestDur = dur; best = m[1]; }
  }
  return best ? (best + ' ' + Math.round(bestDur) + 'ms') : '';
}
