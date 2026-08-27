// Minimal service worker — exists only to satisfy PWA installability criteria
// (Chrome/Edge require a registered service worker with a fetch handler before
// showing the install prompt). This app is NOT going for offline support —
// see issue #12's "out of scope" — so this deliberately does no caching and
// just passes every request straight through to the network.
self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});

// Issue #377 — Web Push. The payload is whatever app/notify.py's WebPushChannel
// sent: {"title": ..., "body": ...} (see that module — same title/body shape every
// other notification channel already uses, just JSON-encoded for the wire).
self.addEventListener('push', (event) => {
  let title = 'AniDex';
  let body = '';
  try {
    const data = event.data ? event.data.json() : {};
    title = data.title || title;
    body = data.body || '';
  } catch {
    body = event.data ? event.data.text() : '';
  }
  event.waitUntil(self.registration.showNotification(title, { body }));
});
