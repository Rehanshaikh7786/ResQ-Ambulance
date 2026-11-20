/* Service worker: keeps the app shell + any map tiles you've viewed available offline. Never caches API calls. */
const V = 'sos-v2', SHELL = ['/report', '/static/css/app.css', '/static/js/common.js', '/static/js/maps.js', '/static/js/i18n.js', '/static/js/report.js', '/static/vendor/leaflet.js', '/static/vendor/leaflet.css'];
self.addEventListener('install', e => { e.waitUntil(caches.open(V).then(c => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== 'GET' || u.origin !== location.origin || u.pathname.startsWith('/api/') || u.pathname.startsWith('/uploads/')) return;
  if (u.pathname.startsWith('/tiles/') || u.pathname.startsWith('/static/')) {          // cache-first
    e.respondWith(caches.match(r).then(hit => hit || fetch(r).then(res => { if (res.ok) { const cp = res.clone(); caches.open(V).then(c => c.put(r, cp)); } return res; })));
  } else if (r.mode === 'navigate') {                                                   // network-first for pages
    e.respondWith(fetch(r).then(res => { if (u.pathname === '/report') { const cp = res.clone(); caches.open(V).then(c => c.put(r, cp)); } return res; }).catch(() => caches.match(r).then(h => h || caches.match('/report'))));
  }
});
