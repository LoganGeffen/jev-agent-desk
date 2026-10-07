self.addEventListener('push', event => {
  let payload = {};
  try { payload = event.data?.json() || {}; } catch {}
  const url = new URL(typeof payload.url === 'string' ? payload.url : '/', self.location.origin);
  const safeUrl = url.origin === self.location.origin ? url.pathname + url.search : '/';
  event.waitUntil(self.registration.showNotification(
    typeof payload.title === 'string' && payload.title ? payload.title : 'Jev Agent Desk', {
      body: typeof payload.body === 'string' && payload.body ? payload.body : 'Agent replied',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      tag: typeof payload.event_id === 'string' ? payload.event_id : undefined,
      data: {url: safeUrl}
    }
  ));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || '/', self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    const existing = windows.find(client => new URL(client.url).origin === self.location.origin);
    if (existing) {
      await existing.navigate(url);
      return existing.focus();
    }
    return self.clients.openWindow(url);
  })());
});
