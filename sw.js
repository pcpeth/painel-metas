/* Service worker do Painel de Metas: abre offline e rápido.
   - Página (index.html): rede primeiro, cai no cache se estiver offline (sempre pega a versão nova quando há internet)
   - Bibliotecas de CDN (Chart.js, Firebase SDK, fontes): cache primeiro
   - Dados do Firebase/Google APIs: nunca passam pelo cache aqui (o Firestore já tem cache próprio) */
const VERSION = "pm-v1";
const SHELL = ["./", "./index.html", "./manifest.json", "./icon-192.png", "./icon-512.png"];
const CDN = ["cdn.jsdelivr.net", "www.gstatic.com", "fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (url.origin === location.origin){
    e.respondWith(
      fetch(req).then(res => { const copy = res.clone(); caches.open(VERSION).then(c => c.put(req, copy)); return res; })
        .catch(() => caches.match(req, {ignoreSearch: true}).then(r => r || caches.match("./index.html")))
    );
    return;
  }
  if (CDN.includes(url.hostname)){
    e.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        if (res.ok || res.type === "opaque"){ const copy = res.clone(); caches.open(VERSION).then(c => c.put(req, copy)); }
        return res;
      }))
    );
  }
});
