// Minimal DOM stub + loader that runs the real static/js modules and the classic app.js in one Node context.
// No jsdom (it would need npm install); elements are permissive recorders, enough for behavior/smoke tests.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';

const STATIC = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..', 'taskqueue', 'static');

class ClassList {
  constructor(el) { this.el = el; this.s = new Set(); }
  add(...c) { c.forEach(x => this.s.add(x)); this.sync(); }
  remove(...c) { c.forEach(x => this.s.delete(x)); this.sync(); }
  toggle(c, force) { const on = force === undefined ? !this.s.has(c) : !!force; on ? this.s.add(c) : this.s.delete(c); this.sync(); return on; }
  contains(c) { return this.s.has(c); }
  has(c) { return this.s.has(c); }
  sync() { this.el._class = [...this.s].join(' '); }
}

export class El {
  constructor(doc, tag = 'div', id = '') {
    this.ownerDocument = doc; this.tagName = tag.toUpperCase(); this.id = id; this.children = [];
    this.parentNode = null; this.style = {}; this.dataset = {}; this.attrs = {}; this.listeners = {};
    this.classList = new ClassList(this); this._class = ''; this._html = ''; this.textContent = '';
    this.value = ''; this.checked = false; this.hidden = false; this.disabled = false;
    this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; this.offsetWidth = 0;
  }
  get className() { return this._class; }
  set className(v) { this.classList.s = new Set(String(v).split(/\s+/).filter(Boolean)); this.classList.sync(); }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.children = []; this.ownerDocument._parseInto(this, this._html); }
  get parentElement() { return this.parentNode && this.parentNode.tagName !== 'BODY' ? this.parentNode : this.parentNode; }
  get firstChild() { return this.children[0] || null; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter(x => x !== f); }
  dispatchEvent(ev) { ev.target ||= this; ev.currentTarget = this; (this.listeners[ev.type] || []).forEach(f => f(ev)); return true; }
  click() { this.dispatchEvent({ type: 'click', target: this, bubbles: true, preventDefault() {}, stopPropagation() {} }); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  append(...cs) { cs.forEach(c => typeof c === 'string' ? null : this.appendChild(c)); }
  prepend(c) { c.parentNode = this; this.children.unshift(c); }
  insertBefore(c) { return this.appendChild(c); }
  insertAdjacentHTML(pos, h) { this._html = pos === 'afterbegin' || pos === 'beforebegin' ? h + this._html : this._html + h; }
  removeChild(c) { c.remove(); return c; }
  replaceWith(n) { const p = this.parentNode; if (!p) return; const i = p.children.indexOf(this); n.parentNode = p; p.children[i] = n; this.parentNode = null; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this); this.parentNode = null; }
  contains(o) { for (let n = o; n; n = n.parentNode) if (n === this) return true; return false; }
  closest(sel) { for (let n = this; n; n = n.parentNode) if (n.matches && n.matches(sel)) return n; return null; }
  matches(sel) {
    return sel.split(',').some(s => { s = s.trim(); if (s.startsWith('#')) return this.id === s.slice(1);
      if (s.startsWith('.')) return this.classList.has(s.slice(1)); const m = /^\[([\w-]+)\]$/.exec(s);
      return m ? this.hasAttribute(m[1]) : this.tagName === s.toUpperCase(); });
  }
  querySelector(sel) { return this.ownerDocument._find(sel, this)[0] || null; }
  querySelectorAll(sel) { return this.ownerDocument._find(sel, this); }
  getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 }; }
  focus() { this.ownerDocument.activeElement = this; }
  blur() {}
  scrollIntoView() {}
  scrollTo() {}
  select() {}
  getContext() { return new Proxy({}, { get: (t, k) => (k in t ? t[k] : () => ({ width: 0 })), set: (t, k, v) => { t[k] = v; return true; } }); }
}

export class Doc {
  constructor() {
    this.byId = new Map(); this.listeners = {}; this.activeElement = null; this.cookie = '';
    this.documentElement = new El(this, 'html'); this.body = new El(this, 'body'); this.head = new El(this, 'head');
    this.readyState = 'complete'; this.hidden = false; this.title = '';
  }
  /** Register an element the page "contains"; tests build the DOM they need (ids are looked up lazily). */
  add(id, { tag = 'div', cls = '', parent = null, attrs = {} } = {}) {
    const el = new El(this, tag, id);
    if (cls) el.className = cls;
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    this.byId.set(id, el);
    (parent || this.body).appendChild(el);
    return el;
  }
  getElementById(id) { return this.byId.get(id) || this.all().find(e => e.id === id) || null; }
  /** Build the element tree (ids, classes, attributes, nesting) from index.html with a tiny tag scanner. */
  loadHtml(html) { this._parseInto(this.body, html); }
  _parseInto(root, html) {
    const unesc = v => v.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
    html = html.replace(/<!--[\s\S]*?-->/g, '').replace(/<(script|style)\b[\s\S]*?<\/\1>/gi, '');
    const VOID = new Set(['meta', 'link', 'input', 'img', 'br', 'hr', 'source', 'col']);
    const stack = [root];
    const re = /<(\/?)([a-zA-Z][\w-]*)((?:\s+[\w:-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>/g;
    let m, last = 0;
    while ((m = re.exec(html))) {
      const [, close, tag, attrs, selfc] = m;
      const txt = html.slice(last, m.index); last = re.lastIndex;
      if (txt.trim() && stack.length > 1) stack[stack.length - 1].textContent += unesc(txt);
      if (close) { if (stack.length > 1) stack.pop(); continue; }
      if (['html', 'head', 'body'].includes(tag.toLowerCase())) continue;
      const el = new El(this, tag);
      for (const a of attrs.matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
        const v = unesc(a[2] ?? a[3] ?? a[4] ?? '');
        if (a[1] === 'class') el.className = v; else el.setAttribute(a[1], v);
        if (a[1] === 'checked' && tag.toLowerCase() === 'input') el.checked = true;
      }
      if (el.id) this.byId.set(el.id, el);
      stack[stack.length - 1].appendChild(el);
      if (!VOID.has(tag.toLowerCase()) && !selfc) stack.push(el);
    }
  }
  createElement(tag) { return new El(this, tag); }
  createTextNode(t) { const e = new El(this, '#text'); e.textContent = t; return e; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter(x => x !== f); }
  dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach(f => f(ev)); return true; }
  all() { const out = []; const walk = n => { out.push(n); n.children.forEach(walk); }; walk(this.body); return out; }
  _find(sel, root) {
    const pool = (root && root !== this.body ? (() => { const o = []; const w = n => n.children.forEach(c => { o.push(c); w(c); }); w(root); return o; })() : this.all());
    const simple = (e, s) => {
      const m = /^([\w-]*)((?:[.#][\w-]+|\[[^\]]+\])*)$/.exec(s);
      if (!m) return false;
      if (m[1] && e.tagName !== m[1].toUpperCase()) return false;
      for (const part of m[2].match(/[.#][\w-]+|\[[^\]]+\]/g) || []) {
        if (part[0] === '.' && !e.classList.has(part.slice(1))) return false;
        if (part[0] === '#' && e.id !== part.slice(1)) return false;
        if (part[0] === '[') {
          const mm = /^\[([\w-]+)(?:([*^$]?=)"?([^"\]]*)"?)?\]$/.exec(part); if (!mm) return false;
          const v = e.getAttribute(mm[1]); if (v === null) return false;
          if (mm[2] === '=' && v !== mm[3]) return false;
          if (mm[2] === '*=' && !v.includes(mm[3])) return false;
        }
      }
      return true;
    };
    // descendant combinators: the last part must match the element, earlier parts some ancestor in order
    const matches = (e, parts) => {
      if (!simple(e, parts[parts.length - 1])) return false;
      let i = parts.length - 2, a = e.parentNode;
      while (i >= 0 && a) { if (simple(a, parts[i])) i--; a = a.parentNode; }
      return i < 0;
    };
    return sel.split(',').flatMap(s => {
      const parts = s.trim().split(/\s+/);
      return pool.filter(e => matches(e, parts));
    });
  }
  querySelector(sel) { return this._find(sel)[0] || null; }
  querySelectorAll(sel) { return this._find(sel); }
}

/**
 * Install browser globals, import js/main.js and (optionally) evaluate app.js as a classic script.
 * `routes` maps URL substring -> JSON (or function(url, opts) -> JSON) for fetch().
 */
export async function setup({ routes = {}, app = false, url = 'http://localhost:8765/?page=queue' } = {}) {
  const doc = new Doc();
  const g = globalThis;
  const u = new URL(url);
  const store = {};
  const sstore = {};
  const timers = [];
  const defineAll = (o, props) => { for (const [k, v] of Object.entries(Object.getOwnPropertyDescriptors(props))) Object.defineProperty(o, k, { ...v, configurable: true }); };
  defineAll(g, {
    window: g, document: doc, fetches: [], timers,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    sessionStorage: { getItem: k => (k in sstore ? sstore[k] : null), setItem: (k, v) => { sstore[k] = String(v); }, removeItem: k => { delete sstore[k]; } },
    location: { search: u.search, pathname: u.pathname, href: u.href, origin: u.origin, hash: '', reload() {} },
    history: { pushed: [], replaced: [], pushState(s, t, h) { this.pushed.push(h); g.location.search = h.startsWith('?') ? h : ''; }, replaceState(s, t, h) { this.replaced.push(h); g.location.search = h.startsWith('?') ? h : ''; } },
    navigator: { userAgent: 'node', clipboard: { writeText: async () => {} }, serviceWorker: { register: async () => ({}), addEventListener() {} }, onLine: true },
    innerWidth: 1280, innerHeight: 800,
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    requestAnimationFrame: f => g.setTimeout(f, 0), cancelAnimationFrame: id => g.clearTimeout(id),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    Notification: undefined, IntersectionObserver: undefined, ResizeObserver: class { observe() {} disconnect() {} },
    EventSource: class { constructor(u) { this.url = u; this.readyState = 1; this.closed = false; (g.eventSources ||= []).push(this); } close() { this.closed = true; this.readyState = 2; } addEventListener() {} emit(d) { this.onmessage && this.onmessage({ data: d }); } fail() { this.onerror && this.onerror({}); } static get CLOSED() { return 2; } },
    addEventListener(t, f) { (g._winListeners ||= {})[t] = [...((g._winListeners || {})[t] || []), f]; },
    removeEventListener() {}, dispatchWinEvent(ev) { ((g._winListeners || {})[ev.type] || []).forEach(f => f(ev)); },
    confirm: () => true, alert() {}, prompt: () => null, open() {},
    // never let background timers keep node alive or run the page's polling loops
    setInterval: () => 0, clearInterval() {},
    // timers are recorded, never fired on their own; tests call g.fireTimers(maxDelay) explicitly
    setTimeout: (fn, ms = 0, ...a) => { const t = { id: timers.length + 1, fn, ms, a, live: true }; timers.push(t); return t.id; },
    clearTimeout: id => { const t = timers.find(x => x.id === id); if (t) t.live = false; },
    fireTimers(maxMs = 1e9) { const due = timers.filter(t => t.live && t.ms <= maxMs); due.forEach(t => { t.live = false; t.fn(...t.a); }); return due.length; },
    evalApp: code => vm.runInThisContext(code),
    flush: async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); },
    fetch: async (input, opts = {}) => {
      const href = String(input); g.fetches.push({ url: href, opts });
      for (const [k, v] of Object.entries(routes)) {
        if (href.includes(k)) {
          const body = typeof v === 'function' ? await v(href, opts) : v;
          if (body && body.__status) return { ok: body.__status < 400, status: body.__status, headers: { get: () => null }, json: async () => body.body || {}, text: async () => '' };
          return { ok: true, status: 200, headers: { get: () => null }, json: async () => structuredClone(body), text: async () => JSON.stringify(body) };
        }
      }
      return { ok: false, status: 404, headers: { get: () => null }, json: async () => ({}), text: async () => '' };
    },
  });
  doc.loadHtml(fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8'));
  vm.runInThisContext(fs.readFileSync(path.join(STATIC, 'i18n.js'), 'utf8'), { filename: 'i18n.js' });
  await import(pathToFileURL(path.join(STATIC, 'js', 'main.js')).href + '?t=' + Date.now());
  if (app) {
    const src = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');
    vm.runInThisContext(src, { filename: 'app.js' });
  }
  return { doc, g, S: g.tqState };
}
