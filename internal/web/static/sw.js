/* ═══════════════════════════════════════════════════════════════════
   ClipSync Service Worker
   Minimal app-shell cache for the SPA dashboard.

   Strategy:
     - Navigations (the HTML document): network-first, cache fallback.
     - Static shell assets (js/css/vendor/components/locales/manifest/icons):
       cache-first, network on miss.
     - API and WebSocket requests are never intercepted.

   The server token-protects static files, so every cached request carries
   `?token=...` in its URL. We cache only *successful* responses, so a 403
   from a stale/invalid token can never poison the cache. If the token
   changes, the request URLs change too and the cache simply misses back to
   the network.
   ═══════════════════════════════════════════════════════════════════ */

'use strict';

// v2: shell assets are network-first (the app updates in place on 127.0.0.1,
// so cache-first served stale JS/CSS after an update forever) and the version
// bump purges v1 caches — including any cached copy of the removed
// quickpaste.html, which the old navigation cache-fallback kept serving after
// the file was deleted.
var CACHE = 'clipsync-shell-v2';

self.addEventListener('install', function () {
  // No precaching: we only cache what the page actually loaded successfully,
  // so token-auth quirks can't fill the cache with 403 bodies.
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k !== CACHE; })
          .map(function (k) { return caches.delete(k); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

function isShellAsset(pathname) {
  return pathname === '/manifest.json' ||
    pathname === '/icon-192.png' ||
    pathname === '/icon-512.png' ||
    pathname.indexOf('/js/') === 0 ||
    pathname.indexOf('/css/') === 0 ||
    pathname.indexOf('/vendor/') === 0 ||
    pathname.indexOf('/components/') === 0 ||
    pathname.indexOf('/locales/') === 0;
}

self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return;

  var url;
  try { url = new URL(request.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf('/api/') === 0 || url.pathname === '/ws') return;

  // HTML document — network-first, fall back to the cached shell offline.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then(function (response) {
          if (response.ok) {
            caches.open(CACHE).then(function (cache) {
              cache.put(request, response.clone());
              // Also keep a token-less copy so offline fallback can match
              // both "/" and "/index.html" regardless of the query string.
              cache.put(new Request(url.pathname), response.clone());
            });
          }
          return response;
        })
        .catch(function () {
          return caches.match(request).then(function (hit) {
            return hit || caches.match('/index.html') || caches.match('/');
          });
        })
    );
    return;
  }

  // Static shell asset — network-first, fall back to the cache only offline.
  // The server is local (127.0.0.1), so the extra round-trip is negligible,
  // and an in-place app update immediately serves the fresh JS/CSS instead of
  // an indefinitely-stale cached copy (the "still the quickpaste page after
  // update" bug was cache-first feeding old components forever).
  if (isShellAsset(url.pathname)) {
    event.respondWith(
      fetch(request)
        .then(function (response) {
          if (response.ok) {
            caches.open(CACHE).then(function (cache) { cache.put(request, response.clone()); });
          }
          return response;
        })
        .catch(function () {
          return caches.match(request);
        })
    );
  }
  // Everything else (downloads, QR data, …) is left to the browser.
});
