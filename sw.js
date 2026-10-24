// Réseau d'abord (toujours la version du jour), cache seulement si pas de connexion.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET" || new URL(e.request.url).origin !== location.origin) return;
  e.respondWith(
    fetch(e.request)
      .then((r) => { const c = r.clone(); caches.open("films-v1").then((x) => x.put(e.request, c)); return r; })
      .catch(() => caches.match(e.request))
  );
});
