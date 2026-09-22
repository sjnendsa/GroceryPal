/* Service worker — makes the installed (home-screen) app open instantly and
   keep working offline with the last prices it saw.
   - Our own files (page, store data, nutrition shards): network first, so the
     daily sync shows up right away; falls back to the cached copy when the
     network is slow (>4s) or gone.
   - Pinned third-party libraries (Chart.js, Leaflet, fonts): cache first —
     their URLs are versioned and never change.
   Bump VERSION when this file's caching rules change. */
const VERSION = 'gp-v1';
const SHELL = ['./', 'index.html', 'static_api.js', 'data/stores.js', 'manifest.webmanifest',
               'icons/icon-192.png', 'icons/apple-touch-icon.png'];
const CDN = /^https:\/\/(cdn\.jsdelivr\.net|unpkg\.com|fonts\.googleapis\.com|fonts\.gstatic\.com)\//;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function networkFirst(req) {
  const cached = () => caches.match(req, { ignoreSearch: true });
  const net = fetch(req).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(VERSION).then(c => c.put(req, copy)); }
    return res;
  });
  const slow = new Promise(r => setTimeout(r, 4000)).then(cached);
  // whichever gives a usable response first; the network still refreshes the cache
  return Promise.race([net.catch(cached), slow.then(r => r || net)])
    .then(r => r || net)
    .catch(() => cached().then(r => r || Response.error()));
}

function cacheFirst(req) {
  return caches.match(req).then(hit => hit || fetch(req).then(res => {
    if (res.ok || res.type === 'opaque') { const copy = res.clone(); caches.open(VERSION).then(c => c.put(req, copy)); }
    return res;
  }));
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin === self.location.origin) e.respondWith(networkFirst(req));
  else if (CDN.test(req.url)) e.respondWith(cacheFirst(req));
  // everything else (map tiles, product images) goes straight to the network
});
