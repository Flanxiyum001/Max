"use strict";

// Offline shell only: no API, audio, approval, memory or credential caching.
const CACHE_NAME = "max-pwa-shell-v1";
const SHELL = [
  "./index.html",
  "./app.css",
  "./app.js",
  "./manifest.webmanifest"
];
const SHELL_PATHS = new Set(SHELL.map(path => new URL(path, self.registration.scope).pathname));

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL)));
});
self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(names =>
      Promise.all(names.filter(name => name.startsWith("max-pwa-shell-") &&
        name !== CACHE_NAME).map(name => caches.delete(name)))
    )
  );
});
self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin ||
      !SHELL_PATHS.has(url.pathname)) return;
  event.respondWith(caches.match(request).then(cached => cached || fetch(request)));
});
// Deliberately no push handler: notification consent and the server adapter
// must be implemented separately with generic, token-free payloads.
