/* tq offline shell. Static assets only: /api/* is never cached (stale queue data would mislead).
   Network-first so a daemon upgrade is picked up immediately; the cache is the offline fallback. */
const VERSION = '__TQ_VERSION__';
const CACHE = 'tq-shell-' + VERSION;
const SHELL = ['/', '/static/app.css', '/static/app.js', '/static/i18n.js', '/static/theme-boot.js',
  '/static/js/main.js', '/static/js/core/api.js', '/static/js/core/actions.js', '/static/js/core/state.js', '/static/js/util/format.js', '/static/js/util/colors.js', '/static/js/util/a11y.js', '/static/js/util/projcolor.js', '/static/js/components/logview.js', '/static/js/components/logpanel.js',
  '/static/js/views/audit.js', '/static/js/views/tokens.js', '/static/js/views/schedule.js', '/static/js/views/alerts.js', '/static/js/views/usage.js', '/static/js/views/history.js', '/static/js/views/queue.js', '/static/js/views/dashboard.js', '/static/js/views/deps.js', '/static/js/views/groups.js', '/static/js/views/resources.js', '/static/js/components/modals.js', '/static/js/core/nav.js', '/static/js/core/loader.js', '/static/js/components/logdock.js', '/static/js/components/runlogs.js', '/static/js/components/runmetrics.js', '/static/js/util/labels.js', '/static/js/components/filterbars.js', '/static/js/views/taskstats.js', '/static/js/components/mobpicker.js', '/static/js/components/shortcuts.js', '/static/js/components/queuefilters.js', '/static/js/components/cfgmodal.js', '/static/js/components/taskactions.js', '/static/js/core/refresh.js', '/static/js/core/url.js', '/static/js/components/sparks.js', '/static/js/views/timeline.js', '/static/js/views/docs.js', '/static/js/components/drawer.js', '/static/js/components/prefs.js', '/static/js/components/notifications.js', '/static/js/components/palette.js', '/static/js/components/columns.js', '/static/error_labels.json',
  '/static/locales/en.json', '/manifest.webmanifest', '/favicon.ico'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => Promise.all(
    SHELL.map(u => fetch(u, { cache: 'no-store' }).then(r => { if (r.ok) return c.put(u, r); }).catch(() => {})))
  ).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k.startsWith('tq-shell-') && k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function cacheable(url) {
  const p = url.pathname;
  return url.origin === self.location.origin
    && (p === '/' || p.startsWith('/static/') || p === '/manifest.webmanifest' || p === '/favicon.ico');
}

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || !cacheable(url)) return;
  e.respondWith(fetch(req).then(r => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
    return r;
  }).catch(() => caches.match(req).then(hit => hit || (req.mode === 'navigate' ? caches.match('/') : undefined)
    || new Response('offline', { status: 503 }))));
});
