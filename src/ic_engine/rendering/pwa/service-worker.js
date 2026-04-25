/**
 * Service Worker - Offline support for InvestorClaw PWA
 * Shared across all deployment modes
 */

const CACHE_NAME = 'investorclaw-v2.1.0';
const ASSETS = [
    '/',
    '/dashboard.html',
    '/assets/styles.css',
    '/assets/charts.js',
    '/assets/charts-extended.js',
    '/assets/app.js',
    '/assets/data-loader.js',
    '/assets/avatars/avatars.js',
    '/manifest.json'
];

// Install event - cache app shell
self.addEventListener('install', event => {
    console.log('[Service Worker] Installing...');
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            console.log('[Service Worker] Caching app shell');
            return cache.addAll(ASSETS).catch(err => {
                console.warn('[Service Worker] Some assets could not be cached:', err);
                // Don't fail on missing assets (some may be dynamically loaded)
                return Promise.resolve();
            });
        })
    );
    self.skipWaiting();
});

// Activate event - clean up old caches
self.addEventListener('activate', event => {
    console.log('[Service Worker] Activating...');
    event.waitUntil(
        caches.keys().then(cacheNames => {
            return Promise.all(
                cacheNames.map(cacheName => {
                    if (cacheName !== CACHE_NAME) {
                        console.log(`[Service Worker] Deleting old cache: ${cacheName}`);
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
    self.clients.claim();
});

// Fetch event - serve from cache, fallback to network
self.addEventListener('fetch', event => {
    // Skip non-GET requests
    if (event.request.method !== 'GET') {
        return;
    }

    // Skip cross-origin requests
    const url = new URL(event.request.url);
    if (url.origin !== location.origin) {
        return;
    }

    event.respondWith(
        caches.match(event.request).then(response => {
            // Return cached response if available
            if (response) {
                return response;
            }

            // Fetch from network
            return fetch(event.request).then(response => {
                // Cache successful responses
                if (response && response.status === 200 && response.type === 'basic') {
                    const responseToCache = response.clone();
                    caches.open(CACHE_NAME).then(cache => {
                        cache.put(event.request, responseToCache);
                    });
                }
                return response;
            }).catch(() => {
                // Offline fallback
                console.log(`[Service Worker] Offline - returning cached version of ${event.request.url}`);

                // Return generic offline page if available
                return caches.match('/offline.html').catch(() => {
                    return new Response(
                        '<html><body><h1>Offline</h1><p>You are offline. Cached data may be available.</p></body></html>',
                        { headers: { 'Content-Type': 'text/html' } }
                    );
                });
            });
        })
    );
});

// Handle messages from clients
self.addEventListener('message', event => {
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});
