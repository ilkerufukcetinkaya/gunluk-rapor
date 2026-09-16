// Yalnız bildirim için. fetch dinleyicisi yok: sayfalar önbelleğe alınmaz.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', olay => olay.waitUntil(self.clients.claim()));

self.addEventListener('push', olay => {
  let veri = {};
  try { veri = olay.data ? olay.data.json() : {}; } catch { veri = {}; }
  const baslik = veri.baslik || 'Günlük Rapor';
  olay.waitUntil(self.registration.showNotification(baslik, {
    body: veri.govde || 'Bugünün raporu hazır bekliyor',
    icon: '/static/ikon-192.png',
    badge: '/static/ikon-192.png',
    data: { url: veri.url || '/' },
  }));
});

self.addEventListener('notificationclick', olay => {
  olay.notification.close();
  const url = (olay.notification.data && olay.notification.data.url) || '/';
  olay.waitUntil((async () => {
    const pencereler = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const pencere = pencereler[0];
    if (pencere) {
      await pencere.focus();
      if ('navigate' in pencere) { try { await pencere.navigate(url); } catch {} }
      return;
    }
    await self.clients.openWindow(url);
  })());
});
