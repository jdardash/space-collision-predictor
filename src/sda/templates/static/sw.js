// Service worker for the static WorldView demo.
//
// PWA setup adapted from satvis (https://github.com/Flowm/satvis, MIT license),
// which ships the same idea: precache the app shell and its bundled data, and
// leave every third-party request alone.
//
// Deliberate limits:
//   - Only same-origin GET requests are handled. Cesium, satellite.js and
//     Plotly come from CDNs and the basemap tiles come from NASA and Esri; those
//     requests never reach respondWith, so nothing cross-origin is ever stored
//     and the caches cannot go stale against a CDN version bump.
//   - This file is only registered when the page is running as the static
//     demo. The live FastAPI app must keep serving fresh data, so it never
//     installs a worker.

var CACHE_VERSION = 'worldview-demo-v5';

// Paths are relative to the worker's own scope, which is the demo root: the
// build copies this file next to index.html precisely so the scope covers the
// whole demo without needing a Service-Worker-Allowed header.
var SHELL = [
  './',
  './index.html',
  './static/js/sgp4-worker.js',
  './static/js/styles.js',
  './static/js/tactical.js',
  './static/js/satsearch.js',
  './static/js/layers.js',
  './static/js/passpanel.js',
  './static/js/agentpanel.js',
  './static/js/isscam.js',
  './static/js/categories.js',
  './static/js/conjactions.js',
  './static/manifest.webmanifest',
  './static/icon.svg',
  './static/icon-maskable.svg',
  './data/tles.json',
  './data/satellite-positions.json',
  './data/satellite-orbits.json',
  './data/config.json',
  './data/conjunction-globe-data.json',
  './data/sensors.json'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_VERSION).then(function (cache) {
      // addAll is all-or-nothing, and one missing optional file should not stop
      // the shell from being cached, so each entry is added on its own.
      return Promise.all(SHELL.map(function (path) {
        return cache.add(new Request(path, { cache: 'reload' })).catch(function () {
          return null;
        });
      }));
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        return key === CACHE_VERSION ? null : caches.delete(key);
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return;

  var url;
  try {
    url = new URL(request.url);
  } catch (e) {
    return;
  }
  if (url.origin !== self.location.origin) return; // CDN and tiles: untouched

  event.respondWith(
    caches.match(request).then(function (cached) {
      var network = fetch(request).then(function (response) {
        if (response && response.status === 200 && response.type === 'basic') {
          var copy = response.clone();
          caches.open(CACHE_VERSION).then(function (cache) {
            cache.put(request, copy);
          });
        }
        return response;
      }).catch(function () {
        return cached || Response.error();
      });
      return cached || network;
    })
  );
});
