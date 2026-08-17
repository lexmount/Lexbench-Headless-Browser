self.addEventListener('install', function (e) { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function (e) {
  var url = new URL(e.request.url);
  if (url.pathname === '/v2/t2a/sw-probe') {
    e.respondWith(new Response('SW:hit-sw-probe', {headers: {'Content-Type': 'text/plain'}}));
  }
});
