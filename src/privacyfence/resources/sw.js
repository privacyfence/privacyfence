// PrivacyFence's service worker. Two jobs:
//
// - Tier 1 (both modes, ADR 0064): host the registration.showNotification()
//   calls the page itself makes (see web_shell.py's own script). Those stay
//   on this machine.
// - Tier 2, web push (org mode only, ADR 0081): show the notification a
//   `push` event carries. Only an org-mode page ever subscribes (web_shell.py,
//   when the server hands it a VAPID key), so in local mode no push event ever
//   arrives here. The server sends the minimal level and nothing more -- a
//   title and "N approval(s) pending" (web_push.minimal_payload) -- and this
//   handler shows only those two strings, whatever else a payload might hold.
//
// A click on either kind opens or focuses /approvals. Nothing here fetches,
// caches, or intercepts requests -- this is deliberately not an
// offline-support service worker.
//
// Served at the origin root (GET /sw.js, see web/routes_approvals.py) so
// its default scope covers the whole app -- a service worker's scope can
// never be wider than the path it's served from.

self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
  var title = "PrivacyFence";
  var body = "An approval is waiting";
  try {
    var payload = event.data ? event.data.json() : null;
    if (payload && typeof payload.title === "string" && payload.title) { title = payload.title.slice(0, 60); }
    if (payload && typeof payload.body === "string" && payload.body) { body = payload.body.slice(0, 120); }
  } catch (e) {}
  // Every push must show a notification (userVisibleOnly): browsers revoke
  // the subscription of a site that receives pushes silently.
  event.waitUntil(
    self.registration.showNotification(title, { body: body, tag: "pf-approvals", renotify: true })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(function (clientList) {
        var target = null;
        for (var i = 0; i < clientList.length; i++) {
          var client = clientList[i];
          if (!("focus" in client)) { continue; }
          // An /approvals tab already open is the one to bring forward.
          if (new URL(client.url).pathname === "/approvals") { return client.focus(); }
          if (target === null) { target = client; }
        }
        if (target !== null) {
          return target.focus().then(function (focused) {
            var c = focused || target;
            if ("navigate" in c) { return c.navigate("/approvals"); }
          });
        }
        if (self.clients.openWindow) {
          return self.clients.openWindow("/approvals");
        }
      })
  );
});
