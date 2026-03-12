/**
 * sw.js — Service Worker for the Holded Dashboard SPA.
 *
 * Strategy: network-first with cache fallback.
 * Static assets are updated in the background and served fresh on next load.
 * API calls bypass the SW entirely (handled by fetch interceptor in app.js).
 */
const CACHE_NAME = 'holded-dashboard-v4';

const PRECACHE_URLS = [
  '/',
  '/static/manifest.json'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  /* API calls, non-GET requests, and cross-origin requests bypass SW */
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/') || event.request.method !== 'GET') {
    return;
  }

  /* Network-first: serve fresh content, fall back to cache if offline */
  event.respondWith(
    fetch(event.request).then((response) => {
      if (response.ok) {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
      }
      return response;
    }).catch(() => caches.match(event.request))
  );
});
