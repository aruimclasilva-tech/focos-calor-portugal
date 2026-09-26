// Service Worker mínimo — só o necessário para a app ser instalável e
// abrir (com a última versão guardada) mesmo sem ligação à internet.
// Os dados em si (FIRMS, MTG) são sempre pedidos em rede quando disponível;
// isto não os põe em cache, para nunca mostrares dados propositadamente
// desatualizados sem aviso.

const CACHE_NAME = 'focos-calor-shell-v1';
const SHELL_FILES = [
  './focos_calor_portugal.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const isShellFile = SHELL_FILES.some((f) => url.pathname.endsWith(f.replace('./', '')));

  if (!isShellFile) {
    // Dados (JSON, APIs externas): sempre rede, nunca cache.
    return;
  }

  // Ficheiros da própria app: tenta rede primeiro (para apanhares
  // atualizações), usa a cópia em cache só se estiveres offline.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const clone = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});
