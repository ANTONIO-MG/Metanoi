/**
 * @file Metanoia desktop alerts — the service worker behind the bell's "Desktop
 * alerts" switch. A Django template (the ``icon`` context variable is the brand icon URL),
 * served at /sw.js by apps.communication.views.service_worker.
 *
 * A push from apps/communication/webpush.py becomes a system notification,
 * even with no Metanoia tab open. When a Metanoia tab is already in front, that
 * page shows its own toast and chime, so the worker stays quiet. Clicking the
 * notification focuses a Metanoia tab (or opens one) on the e-mail.
 *
 * Globals: ``self`` (the ServiceWorkerGlobalScope); exports nothing.
 */
/** Activates a new worker version straight away instead of waiting for old tabs to close. */
self.addEventListener('install', function () { self.skipWaiting(); });
/** Takes control of already-open Metanoia tabs on activation. */
self.addEventListener('activate', function (event) { event.waitUntil(self.clients.claim()); });

/**
 * Shows a push payload (``{title, body, tag, url}``, JSON or plain text) as a
 * system notification, unless a visible, focused Metanoia tab will show it itself.
 * A ``tag`` collapses repeats and re-alerts on replacement.
 */
self.addEventListener('push', function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: event.data && event.data.text() }; }
  var title = data.title || 'Metanoia Group';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
    var inFront = list.some(function (c) { return c.visibilityState === 'visible' && c.focused; });
    if (inFront) return null;
    return self.registration.showNotification(title, {
      body: data.body || '',
      icon: '{{ icon }}',
      badge: '{{ icon }}',
      tag: data.tag || undefined,
      renotify: !!data.tag,
      data: { url: data.url || '/' }
    });
  }));
});

/**
 * On click, focuses an existing Metanoia tab and navigates it to the
 * notification's ``url``, or opens a new window when none is open.
 */
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (c.url.indexOf(self.location.origin) === 0 && 'focus' in c) {
        return c.focus().then(function (w) { return w && w.navigate ? w.navigate(url) : null; });
      }
    }
    return self.clients.openWindow ? self.clients.openWindow(url) : null;
  }));
});
