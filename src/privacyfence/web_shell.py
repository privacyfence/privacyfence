"""Shared page chrome for the web surfaces (one header, one nav, one
palette, one session, links both ways) -- one pure function, ``wrap()``,
that both ``/approvals`` and
``/settings`` wrap themselves in at the route layer (web/routes_approvals.py,
web/routes_settings.py), so the two pages read as one application instead of
two applications bolted together.

Deliberately **not** used by:

- the native settings window (settings_window.py's WKWebView) or the native
  approval window (approval_window.py's) -- both load their own document's
  markup directly via ``loadHTMLString_baseURL_``, with no HTTP request and
  no other page to link to. Wrapping their shared documents
  (settings_window_html.build_html/approval_window_html.build_card_stack_
  html) in this shell would change what those two already-tested,
  geometry-tuned documents render, for a native host that has no use for a
  cross-page nav bar at all.
- an individual approval card's own page (``GET /approvals/{id}``) -- that
  page *is* the decision screen, full-window, same as the native dialog it
  replaces; the list it returns to is where the shell belongs, not the card itself.

Owns the one thing every shell-wrapped page needs and none of them should
reimplement: the ``/api/state/stream`` SSE connection (web/state_stream.py)
that drives the live indicator and dispatches each event to whichever of
``window.__pfRender``/``window.__pfRenderApprovals`` the current page
happens to define -- settings_window_html.py's own bridge JS already
defines the former (unchanged, since it also serves the native-window
push path); approval_list_html.py defines the latter. Centralizing the
connection here, rather than duplicating an EventSource per page, is what
makes "one live indicator" true instead of aspirational.
"""
from __future__ import annotations

import json
import secrets
from html import escape as _html_escape
from pathlib import Path

from . import approval_icons
from .design_css import DOCUMENT_CSS

# Browser-tab favicon -- the same bundled shield mark approval_icons.py
# already hands the native/web approval cards (resources/icon_32.png, the
# size a favicon is actually rendered at), embedded as a data: URI so this
# shared shell never needs its own unauthenticated route or asset file just
# to satisfy the browser's automatic GET /favicon.ico.
_FAVICON_DATA_URI = approval_icons.icon_data_uri(
    str(Path(__file__).parent / "resources" / "icon_32.png")
)
# The header's brand mark, as the website's header shows its icon: the same shield, at twice the
# size it is drawn so it stays sharp on a 2x screen.
_BRAND_ICON_DATA_URI = approval_icons.icon_data_uri(
    str(Path(__file__).parent / "resources" / "icon_64.png")
)

# The header is the website's (website/_partials/header.html and website/chrome.css): a floating,
# rounded, sticky bar with the brand mark on the left and the navigation on the right, whose
# links collapse into a <details> menu when there is no room for them. The website switches at a
# 900 px viewport; the app has no viewport breakpoints (shared rule 6, base.css), so the same
# switch is a container query on .pf-shell-top, the header's full-width wrapper. Everything is
# tokens, so app.css's dark mode applies without a rule here.
_SHELL_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--ink); font-family: var(--font-sans);
  font-size: 14px; min-height: 100vh; display: flex; flex-direction: column;
  -webkit-font-smoothing: antialiased;
}
.pf-shell-top { position: sticky; top: 0; z-index: 20; flex-shrink: 0; container-type: inline-size; padding-top: 14px; }
.pf-shell-header {
  position: relative; width: min(1220px, 100% - 2 * var(--gutter) + 12px); margin: 0 auto 14px;
  min-height: 64px; display: flex; align-items: center; gap: var(--space-s);
  padding: 8px 10px 8px 16px; border-radius: 18px;
  background: color-mix(in srgb, var(--surface) 90%, transparent);
  border: 1px solid var(--line); backdrop-filter: blur(16px); box-shadow: var(--shadow-header);
}
.pf-shell-brand {
  display: flex; align-items: center; gap: 10px; min-height: var(--tap); min-width: var(--tap);
  font-weight: 750; font-size: 16px; letter-spacing: -.02em; color: var(--ink); text-decoration: none;
}
.pf-shell-brand img { width: 34px; height: 34px; border-radius: 8px; }
.pf-shell-nav { display: flex; align-items: center; justify-content: flex-end; gap: var(--space-2xs); flex: 1; font-size: var(--step-small); }
.pf-shell-nav-links { display: flex; align-items: center; gap: 4px; }
.pf-shell-nav-item {
  display: inline-flex; align-items: center; min-height: var(--tap); min-width: var(--tap);
  padding: 0 11px; border-radius: var(--radius-s); color: var(--ink-soft); text-decoration: none;
}
.pf-shell-nav-item:hover { color: var(--accent-dark); }
/* The current page: ink, weight and a soft surface, not the colour alone. */
.pf-shell-nav-item.active { color: var(--ink); font-weight: 650; background: var(--surface-soft); }
.pf-shell-menu { display: none; }
.pf-shell-menu summary {
  display: inline-flex; align-items: center; gap: 8px; min-height: var(--tap); min-width: var(--tap);
  padding: 0 12px; border: 1px solid var(--line); border-radius: var(--radius-s);
  background: var(--surface); color: var(--ink); font-weight: 650; cursor: pointer; list-style: none;
}
.pf-shell-menu summary::-webkit-details-marker { display: none; }
.pf-shell-menu summary::before {
  content: ""; width: 16px; height: 12px;
  background: linear-gradient(currentColor 0 0) top / 100% 2px no-repeat,
    linear-gradient(currentColor 0 0) center / 100% 2px no-repeat,
    linear-gradient(currentColor 0 0) bottom / 100% 2px no-repeat;
}
.pf-shell-menu[open] summary::before {
  background: linear-gradient(45deg, transparent 45%, currentColor 45% 55%, transparent 55%),
    linear-gradient(-45deg, transparent 45%, currentColor 45% 55%, transparent 55%);
}
/* Anchored to the header, so it never runs off a narrow screen. */
.pf-shell-menu-panel {
  position: absolute; right: 0; top: calc(100% + 8px); width: min(280px, 100%); display: grid;
  padding: 8px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
  box-shadow: var(--shadow);
}
.pf-shell-menu-panel .pf-shell-nav-item { font-size: 16px; color: var(--ink); padding: 0 12px; border-radius: 8px; }
.pf-shell-menu-panel .pf-shell-nav-item + .pf-shell-nav-item { border-top: 1px solid var(--surface-soft); }
.pf-shell-menu-principal { padding: 10px 12px 4px; font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
.pf-shell-live {
  display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted);
  white-space: nowrap;
}
/* The label always says the state; the dot's colour repeats it. */
.pf-shell-live-dot {
  width: 8px; height: 8px; border-radius: 50%; background: var(--control-line); flex-shrink: 0;
}
.pf-shell-live-dot.live { background: var(--success); }
.pf-shell-live-dot.reconnecting { background: var(--warning); }
.pf-shell-live-dot.down { background: var(--danger); }
/* Who this queue belongs to -- org mode only (see wrap's principal_label);
   local mode doesn't render one here, even on a separated install with more
   than one OS-user principal (ADR 0008) -- each one's own page already only
   ever shows their own queue, it just doesn't caption whose it is. */
.pf-shell-principal {
  font-size: 12px; color: var(--ink-soft); white-space: nowrap;
  padding-left: 10px; border-left: 1px solid var(--line);
}
@container (max-width: 900px) {
  .pf-shell-nav-links, .pf-shell-principal { display: none; }
  .pf-shell-menu { display: block; }
}
@container (max-width: 560px) {
  .pf-shell-top { padding-top: 8px; }
  .pf-shell-header { gap: var(--space-xs); padding: 6px 6px 6px 10px; margin-bottom: 8px; }
  .pf-shell-brand { font-size: 15px; }
}
/* The narrowest phones: the menu toggle and the live indicator show only their icon (the text
   stays for screen readers), and below 350 px so does the brand. */
@container (max-width: 420px) {
  .pf-shell-menu summary { padding: 0; justify-content: center; width: var(--tap); }
  .pf-shell-menu-label, .pf-shell-live-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
}
@container (max-width: 350px) {
  .pf-shell-brand span { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
}
/* Neither the shared files nor anything above styles a bare <a>, so any link
   a page renders outside the nav/banner/notice classes would land on the
   browser's default blue. :where() keeps this at one class's specificity, so a
   page's own link class (a button-styled link, say) still sets its own colour. */
.pf-shell-main :where(a:not(.button)) { color: var(--accent-dark); }
.pf-shell-main :where(a:not(.button)):hover { color: var(--accent); }
/* A link drawn as a shared .button keeps the button's own colours, without an underline. */
.pf-shell-main a.button { text-decoration: none; }
.pf-shell-banner {
  padding: 8px 20px; font-size: 13px; font-weight: 600; text-align: center;
  background: var(--danger); color: var(--surface); flex-shrink: 0;
}
.pf-shell-banner a { color: var(--surface); text-decoration: underline; }
.pf-shell-notice {
  padding: 8px 20px; font-size: 13px; text-align: center; flex-shrink: 0;
  background: var(--accent-soft); color: var(--accent-dark);
  border-bottom: 1px solid var(--line);
  display: flex; align-items: center; justify-content: center; gap: 10px;
}
.pf-shell-notice a { color: inherit; font-weight: 600; }
.pf-shell-notice-close {
  background: none; border: none; cursor: pointer; font-size: 18px; line-height: 1;
  color: inherit; opacity: .6; padding: 0; flex-shrink: 0; min-width: var(--tap); min-height: var(--tap);
}
.pf-shell-notice-close:hover { opacity: 1; }
.pf-shell-main { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.pf-shell-toast {
  position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
  background: var(--ink); color: var(--on-ink);
  padding: 10px 16px; border-radius: var(--radius-s); font-size: 13px;
  max-width: min(480px, calc(100vw - 32px)); box-shadow: var(--shadow);
  opacity: 0; pointer-events: none; transition: opacity .15s ease;
  z-index: 1000;
}
.pf-shell-toast.shown { opacity: 1; }
.pf-shell-notif-prompt {
  bottom: 72px; display: flex; align-items: center; gap: 10px; pointer-events: auto;
}
.pf-shell-notif-enable {
  background: var(--accent); color: var(--on-accent); border: none; border-radius: var(--radius-s);
  font-size: 13px; font-weight: 650; padding: 0 14px; min-height: var(--tap); cursor: pointer; flex-shrink: 0;
}
.pf-sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
"""

# EventSource is same-origin by construction (a relative URL), so it
# automatically carries the pf_session cookie web/routes_approvals.py's/
# web/routes_settings.py's own auth already set on the page load that
# reaches this script -- no separate token plumbing needed here. Dispatch,
# not rendering: this script never touches the DOM for either event's
# *content*, only the live indicator -- window.__pfRender/
# window.__pfRenderApprovals (whichever the current page defines) own their
# own page's markup entirely, same separation settings_window_html.py's own
# bridge already has between "receive a message" and "render it".
#
# Notifications, tiers 0-1 -- no push, nothing leaving the machine (ADR
# 0064; org mode's tier 2 is described at the end of this comment):
#   - tier 0: the document title gains a "(N) " badge and a visually
#     hidden aria-live region announces the count, whenever the approvals
#     event's row count changes -- works with no permission at all.
#   - tier 1: registration.showNotification() via resources/sw.js, fired
#     only when the tab is not focused, rate-limited to one per 5s,
#     grouped into a single "N approvals pending" notification rather than
#     one per row. **Content invariant, enforced by construction,
#     via notificationBody()'s own per-level field allowlist**: at
#     `minimal`, or whenever more than one approval fired at once (there is
#     no "several different named things" copy -- the grouping case
#     above), the body is always the bare count -- "N approval(s) pending".
#     `standard` adds three fields off the single pending row's own
#     summary dict -- connector, tool_name and gate_kind (read/write
#     direction), all safe by construction: gate_kind names a category,
#     tool_name names an MCP tool, never gated content, and connector is
#     the same bare connector key approval_list_html.py's own row kicker
#     already capitalizes and shows unescaped, nothing this function
#     invents access to. `detailed` adds
#     one more field: the row's own `summary` -- the one field that
#     genuinely can carry gated content (an event title, a contact name, a
#     document title -- see approvals.PendingApproval's own docstring on
#     it), which is exactly why it's the one gated behind the highest
#     level rather than shown at `standard` or below.
#   - the permission pre-prompt only ever fires from window.__pfNotifPrompt,
#     called by approval_list_html.py's own script right after a decision
#     is made (never on page load: a cold
#     Notification.requestPermission() is what browsers now penalize).
#
# Tier 2, web push, is org mode's alone (ADR 0081) and is on only when the
# page is given the server's VAPID key (wrap's push_public_key). Then the
# same pre-prompt, once permission is granted, subscribes this browser and
# posts the subscription to /api/push/subscription (web/routes_push.py); a
# page load with permission already granted re-posts it, so the server's
# copy follows the browser's. The push itself is shown by resources/sw.js
# and says only what minimal says: a count. iOS offers push only to a site
# added to the Home Screen, so there, before installation, the pre-prompt
# is replaced by a hint that says so.
_STREAM_JS = """
(function () {
  // web.notifications.enabled (settings.yaml.example) -- config wiring for
  // tiers 0-1 together, per that config block's own comment. false turns
  // off the title badge, the aria-live announcement, service worker
  // registration, and the permission pre-prompt alike; the live-connection
  // indicator above is unaffected (it isn't a notification).
  var NOTIFICATIONS_ENABLED = %(notifications_enabled)s;
  // web.notifications.detail -- "minimal" | "standard" | "detailed". See
  // notificationBody() below for
  // what each level is allowed to read off a pending-approval row.
  var NOTIFICATIONS_DETAIL = %(notifications_detail)s;
  // __pfNotificationsEnabled is exposed globally so the settings page's own
  // notifications card (settings_window_html.py's renderNotificationsCard)
  // can read the same config flag -- that module's JS is shared with the
  // native settings window, which never loads this script at all, so it
  // treats a missing flag as "on" (feature-detecting Notification support
  // instead) rather than assuming this variable exists. There is no
  // equivalent __pfNotificationsDetail read anywhere else: the card's own
  // detail-level control is a real, mutable setting
  // (SettingsController.set_notifications_detail) sourced from that page's
  // own `state.general.notifications_detail` on every render, not from
  // this per-page-load constant -- NOTIFICATIONS_DETAIL below stays local
  // to this closure, used only by notificationBody() to decide what an
  // actual browser notification on *this* page is allowed to say.
  window.__pfNotificationsEnabled = NOTIFICATIONS_ENABLED;
  // Org mode's web push (see this file's comment above _STREAM_JS): the
  // server's VAPID public key, or "" when push is off (always, in local mode).
  var PUSH_PUBLIC_KEY = %(push_public_key)s;
  var PUSH_CSRF = %(push_csrf)s;

  var dot = document.getElementById('pf-shell-live-dot');
  var label = document.getElementById('pf-shell-live-label');
  var announcer = document.getElementById('pf-shell-announcer');
  var baseTitle = document.title;
  var lastCount = null;
  var lastNotifyAt = 0;

  function setState(state, text) {
    if (dot) { dot.className = 'pf-shell-live-dot ' + state; }
    if (label) { label.textContent = text; }
  }

  function updateBadge(count) {
    if (!NOTIFICATIONS_ENABLED) { return; }
    document.title = count > 0 ? '(' + count + ') ' + baseTitle : baseTitle;
    if (announcer) {
      announcer.textContent = count > 0
        ? count + ' approval' + (count === 1 ? '' : 's') + ' pending'
        : 'No approvals pending';
    }
  }

  function countBody(count) {
    return count === 1 ? '1 approval pending' : count + ' approvals pending';
  }

  // The notification-detail allowlist -- see this file's own comment above
  // _STREAM_JS for what each level is allowed to read off `row`. Only ever
  // called with exactly the one row a count-increase-to-1 identifies
  // unambiguously (see
  // maybeNotify below); anything else falls back to the plain count, which
  // is always safe at any level.
  function notificationBody(count, rows) {
    if (NOTIFICATIONS_DETAIL === 'minimal' || count !== 1 || !rows || rows.length !== 1) {
      return countBody(count);
    }
    var row = rows[0];
    var connector = row.connector
      ? row.connector.charAt(0).toUpperCase() + row.connector.slice(1)
      : '';
    var direction = row.gate_kind === 'review' ? 'read' : row.gate_kind === 'popup' ? 'write' : '';
    var head = [connector, row.tool_name || ''].filter(function (s) { return !!s; }).join(' — ');
    if (direction) { head += ' · ' + direction; }
    if (!head) { return countBody(count); }
    if (NOTIFICATIONS_DETAIL === 'detailed' && row.summary) { head += '\\n' + row.summary; }
    return head;
  }

  function maybeNotify(count, rows) {
    if (!NOTIFICATIONS_ENABLED) { return; }
    if (count === 0 || lastCount === null || count <= lastCount) { return; }
    if (typeof document.hasFocus === 'function' && document.hasFocus()) { return; }
    var now = Date.now();
    if (now - lastNotifyAt < 5000) { return; }
    lastNotifyAt = now;
    if (!('Notification' in window) || Notification.permission !== 'granted') { return; }
    if (!('serviceWorker' in navigator)) { return; }
    var body = notificationBody(count, rows);
    navigator.serviceWorker.ready.then(function (reg) {
      reg.showNotification('PrivacyFence', { body: body, tag: 'pf-approvals', renotify: false });
    }).catch(function () {});
  }

  function onApprovalsEvent(rows) {
    var count = (rows || []).length;
    updateBadge(count);
    maybeNotify(count, rows);
    lastCount = count;
  }

  function pushSupported() {
    return !!PUSH_PUBLIC_KEY && 'serviceWorker' in navigator && 'PushManager' in window
      && 'Notification' in window;
  }

  // iPhone and iPad (which reports itself as a Mac with a touch screen)
  // expose web push only to a site opened from the Home Screen.
  function iosNotInstalled() {
    var ua = navigator.userAgent || '';
    var ios = /iPhone|iPad|iPod/.test(ua)
      || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    return ios && navigator.standalone !== true;
  }

  function keyBytes(b64url) {
    var s = b64url.replace(/-/g, '+').replace(/_/g, '/');
    while (s.length %% 4) { s += '='; }
    var raw = atob(s);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) { out[i] = raw.charCodeAt(i); }
    return out;
  }

  function sameKey(buffer, bytes) {
    if (!buffer) { return false; }
    var a = new Uint8Array(buffer);
    if (a.length !== bytes.length) { return false; }
    for (var i = 0; i < a.length; i++) { if (a[i] !== bytes[i]) { return false; } }
    return true;
  }

  function subscribePush() {
    if (!pushSupported() || Notification.permission !== 'granted') { return; }
    var key = keyBytes(PUSH_PUBLIC_KEY);
    navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.getSubscription().then(function (existing) {
        // A subscription made for another server key cannot receive this
        // server's pushes; replace it rather than posting a dead one.
        if (existing && existing.options && !sameKey(existing.options.applicationServerKey, key)) {
          return existing.unsubscribe().then(function () { return null; });
        }
        return existing;
      }).then(function (existing) {
        return existing || reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
      });
    }).then(function (sub) {
      return fetch('/api/push/subscription', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({subscription: sub.toJSON(), csrf: PUSH_CSRF})
      });
    }).catch(function () {});
  }

  function promptBar(message, buttonLabel, onClick) {
    var bar = document.createElement('div');
    bar.className = 'pf-shell-toast pf-shell-notif-prompt shown';
    bar.setAttribute('role', 'status');
    var text = document.createElement('span');
    text.textContent = message;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pf-shell-notif-enable';
    btn.textContent = buttonLabel;
    btn.addEventListener('click', function () {
      if (onClick) { onClick(); }
      bar.remove();
    });
    bar.appendChild(text);
    bar.appendChild(btn);
    document.body.appendChild(bar);
    setTimeout(function () { if (bar.parentNode) { bar.remove(); } }, 10000);
    return bar;
  }

  function onceIn(key) {
    var already;
    try { already = localStorage.getItem(key); } catch (e) { already = null; }
    if (already) { return false; }
    try { localStorage.setItem(key, '1'); } catch (e) {}
    return true;
  }

  if ((NOTIFICATIONS_ENABLED || PUSH_PUBLIC_KEY) && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }
  if (PUSH_PUBLIC_KEY) { subscribePush(); }

  window.__pfNotifPrompt = function () {
    if (!NOTIFICATIONS_ENABLED && !PUSH_PUBLIC_KEY) { return; }
    if (PUSH_PUBLIC_KEY && iosNotInstalled()) {
      if (!onceIn('pf_push_ios_hint')) { return; }
      promptBar(
        'Add PrivacyFence to your Home Screen to get notifications: tap Share, then Add to Home Screen.',
        'OK', null
      ).id = 'pf-shell-ios-push-hint';
      return;
    }
    if (!NOTIFICATIONS_ENABLED && !pushSupported()) { return; }
    if (!('Notification' in window) || Notification.permission !== 'default') { return; }
    if (!onceIn('pf_notif_prompted')) { return; }
    promptBar('Want PrivacyFence to notify you when Claude needs approval?', 'Enable', function () {
      var asked = Notification.requestPermission();
      if (asked && asked.then) { asked.then(function () { subscribePush(); }); }
    });
  };

  // The dismissible notice strip is
  // rendered fresh on every request just like the banner above it, but
  // unlike the banner it's an invitation, not a live state indicator --
  // once a viewer dismisses it in a given browser, it stays gone there
  // (localStorage, guarded the same way pf_notif_prompted already is
  // below, for the same private-browsing/storage-disabled reason) even
  // though the server keeps rendering it on the next request until the
  // underlying config actually changes.
  (function () {
    var notice = document.getElementById('pf-shell-notice');
    if (!notice) { return; }
    var key = notice.getAttribute('data-dismiss-key');
    var dismissed;
    try { dismissed = key ? localStorage.getItem(key) : null; } catch (e) { dismissed = null; }
    if (dismissed) { notice.remove(); return; }
    var closeBtn = notice.querySelector('[data-dismiss-notice]');
    if (closeBtn) {
      closeBtn.addEventListener('click', function () {
        try { if (key) { localStorage.setItem(key, '1'); } } catch (e) {}
        notice.remove();
      });
    }
  })();

  setState('reconnecting', 'connecting…');
  if (typeof EventSource === 'undefined') {
    setState('down', "can't reach PrivacyFence");
    return;
  }
  var es = new EventSource(%(stream_url)s);
  es.onopen = function () { setState('live', 'live'); };
  es.onerror = function () {
    // readyState CLOSED means the browser gave up for good --
    // a non-2xx response (this route's own 401 once the session has
    // idle-/absolute-expired) never gets an automatic retry per the
    // EventSource spec, unlike a transient network error, which leaves
    // readyState CONNECTING while it retries on its own. Telling those
    // two apart is what stops "reconnecting…" from lying forever on an
    // expired tab that in fact needs a fresh bootstrap link, not a wait.
    if (es.readyState === EventSource.CLOSED) {
      setState('down', 'session expired — reopen this page');
    } else {
      setState('reconnecting', 'reconnecting…');
    }
  };
  es.addEventListener('settings', function (e) {
    if (window.__pfRender) { window.__pfRender(JSON.parse(e.data)); }
  });
  es.addEventListener('approvals', function (e) {
    var rows = JSON.parse(e.data);
    if (window.__pfRenderApprovals) { window.__pfRenderApprovals(rows); }
    onApprovalsEvent(rows);
  });
  window.__pfStateStream = es;
})();
"""

_NAV_ITEMS = (("approvals", "Approvals", "/approvals"), ("settings", "Settings", "/settings"))

# Org mode's own route set. It has no local-mode ``/settings`` dispatcher
# (see web/server.py's module docstring for what org mode deliberately
# doesn't mount -- ``/settings`` there is routes_settings.build_org_routes's own,
# much smaller surface), and ``/connect``/``/security`` are surfaces local
# mode has no equivalent of.
ORG_NAV_ITEMS = (
    ("approvals", "Approvals", "/approvals"),
    ("connections", "Connections", "/connect"),
    ("passkeys", "Passkeys", "/security"),
    ("settings", "Settings", "/settings"),
)

# The installable org app (ADR 0081): the manifest, and the icon iOS uses for the Home Screen,
# which it reads from this link rather than from the manifest. Both routes are org mode's
# alone (web/routes_push.py).
_ORG_APP_LINKS = (
    '\n<link rel="manifest" href="/manifest.webmanifest">'
    '\n<link rel="apple-touch-icon" href="/icons/icon-192.png">'
)


def _nav_html(active: str, nav_items: tuple[tuple[str, str, str], ...]) -> str:
    items = []
    for key, label, href in nav_items:
        cls = "pf-shell-nav-item active" if key == active else "pf-shell-nav-item"
        current = ' aria-current="page"' if key == active else ""
        items.append(f'<a class="{cls}" href="{href}"{current}>{_html_escape(label)}</a>')
    return "".join(items)


def header_html(
    active: str, nav_items: tuple[tuple[str, str, str], ...] = _NAV_ITEMS, *,
    live_html: str = "", principal_label: str = "",
) -> str:
    """The shell's header, in the website's markup pattern (website/_partials/header.html): the
    brand mark, the links inline, and the same links again in a ``<details>`` menu that replaces
    them when the header is narrow (``_SHELL_CSS``'s container queries). Keep the two lists
    equal; both come from ``nav_items``. The signed-in principal, when there is one, is shown in
    the header when there is room and at the top of the menu when there is not."""
    links = _nav_html(active, nav_items)
    home = nav_items[0][2] if nav_items else "/"
    principal_html = principal_menu_html = ""
    if principal_label:
        label = _html_escape(principal_label)
        principal_html = f'<div class="pf-shell-principal">{label}</div>'
        principal_menu_html = f'<div class="pf-shell-menu-principal">Signed in as {label}</div>'
    return (
        '<div class="pf-shell-top"><header class="pf-shell-header">'
        f'<a class="pf-shell-brand" href="{home}" aria-label="PrivacyFence home">'
        f'<img src="{_BRAND_ICON_DATA_URI}" alt="" width="34" height="34"><span>PrivacyFence</span></a>'
        '<nav class="pf-shell-nav" aria-label="Primary navigation">'
        f'<div class="pf-shell-nav-links">{links}</div>'
        '<details class="pf-shell-menu"><summary><span class="pf-shell-menu-label">Menu</span></summary>'
        f'<div class="pf-shell-menu-panel">{principal_menu_html}{links}</div></details>'
        '</nav>'
        f'{live_html}{principal_html}'
        '</header></div>'
    )


def wrap(
    body_html: str, *, title: str, active: str, nonce: str | None = None,
    notifications_enabled: bool = True, notifications_detail: str = "minimal",
    banner_html: str | None = None,
    dismissible_notice_html: str | None = None, dismissible_notice_key: str = "",
    nav_items: tuple[tuple[str, str, str], ...] = _NAV_ITEMS,
    principal_label: str = "",
    live_updates: bool = True,
    stream_url: str = "/api/state/stream",
    push_public_key: str = "",
    csrf: str = "",
) -> str:
    """Full ``<!DOCTYPE html>`` document: the design files + the shell's own CSS,
    the header (brand, nav between Approvals/Settings, live indicator), and
    ``body_html`` dropped into ``<main>`` unescaped -- callers own their own
    content's escaping, same convention web/routes_approvals.py's existing
    HTML-building routes already follow throughout this codebase.

    ``active`` is one of ``"approvals"``/``"settings"`` -- which nav item
    renders as current. ``notifications_enabled`` is settings.yaml.
    example's ``web.notifications.enabled`` (default true) -- false turns
    off tiers 0 and 1 together (title badge, aria-live announcement,
    service worker registration, the permission pre-prompt), per that
    config key's own comment. ``notifications_detail`` is that same config
    block's ``detail`` (``"minimal"``/``"standard"``/``"detailed"``) -- see
    _STREAM_JS's own
    notificationBody() for exactly what each level is allowed to say.

    ``nonce``: the
    current response's CSP nonce (``request.state.csp_nonce``, set by
    web/server.py's ``_SecurityHeadersMiddleware``) -- this document is
    rendered fresh on every request, so unlike approval_window_html.py's
    card documents it always takes the caller's nonce rather than minting
    its own. Also the nonce ``body_html`` itself must have used for its own
    ``<style>``/``<script>`` tags (approval_list_html.build_list_html's own
    ``nonce`` parameter) -- one document, one Content-Security-Policy
    header, one nonce. Defaults to a fresh one when omitted (every real
    caller passes the actual per-request value explicitly).

    ``banner_html``: an already-escaped fragment shown as a
    full-width, non-dismissable strip between the header and ``<main>`` --
    ``None`` (the default) renders nothing. The one real caller today is
    web/routes_approvals.py's/web/routes_settings.py's own
    ``step_up.require_passkey`` check: with that flag on and no passkey
    enrolled, the daemon starts and keeps serving (step_up_config.py's own
    "closed for releases, open for repair" -- refusing to boot would remove
    the only path to ``/security``, the one page that can fix this; ADR 0069), but
    every approving decision and every sensitive settings action hard-fails
    (webauthn_stepup.has_credentials() is False, so decide()/settings_
    action() both 403 rather than release anything) -- this banner is what
    makes that state visible on every page rather than only discoverable by
    triggering the 403 itself. Rendered on every request fresh, so it
    reflects the current enrollment state, not a dismissed-once flag: it
    disappears the moment a passkey is enrolled, with no separate
    acknowledgement step.

    ``dismissible_notice_html``: a second,
    lower-priority strip below ``banner_html`` for a fact that's worth
    surfacing once but isn't itself a live problem -- today, step_up_
    config.py's own ``off_notice()``: an install that has simply never
    turned step-up on. Unlike ``banner_html`` this *is* a dismissed-once
    flag: a viewer who closes it won't see it again in that browser
    (localStorage, client-side -- see this module's own ``_STREAM_JS``),
    even on a later request where the server would render it again,
    because the underlying condition (step-up still off) hasn't itself
    changed. ``dismissible_notice_key`` is the localStorage key that
    dismissal is recorded under, and must be a real, distinguishing string
    whenever ``dismissible_notice_html`` is given -- distinct notices need
    distinct keys or dismissing one silently dismisses the other too.

    ``nav_items``/``principal_label``/``live_updates`` are what let org
    mode share this shell rather than serve a bare document. Local
    mode passes none of them.

    ``nav_items`` is ``(key, label, href)`` per item, matched against
    ``active`` -- ``ORG_NAV_ITEMS`` above is org mode's set.
    ``principal_label`` renders the signed-in principal at the right of the
    header; org mode's entire authorization model is per-principal and its
    approvals page otherwise never says whose queue is being looked at.
    Empty (the default) renders nothing, which is right for local mode,
    where there is only ever one.

    ``live_updates=False`` omits both the live indicator and the
    ``EventSource`` script. The indicator is not decoration -- it tells a
    reviewer whether the queue in front of them is current -- so it must
    not render in a mode that has no stream behind it: org mode's app
    (web/server.py's ``_build_org_app``) mounts no
    ``GET /api/state/stream`` at all, and a dot that says "live" against
    nothing, or sits permanently on "can't reach PrivacyFence" against a
    404, is worse than no dot on the one surface whose whole job is to be
    trusted. The post-decision toast is unaffected: it is driven by the
    list page's own script, not this one.

    ``stream_url`` is the SSE endpoint the ``EventSource`` connects to when
    ``live_updates`` is on. Local mode keeps the default
    ``/api/state/stream``; org mode's approvals page passes
    ``/api/approvals/stream`` (web/routes_approvals.py), which carries the
    same ``approvals`` event for just the signed-in principal and no
    ``settings`` event -- the org pages with no stream at all (connect,
    security, settings) still pass ``live_updates=False``.

    Org mode (``nav_items`` is ``ORG_NAV_ITEMS``) also links the Web App
    Manifest and its icon (web/routes_push.py), which makes the org app
    installable; local mode serves neither route and links neither.
    ``push_public_key`` is org mode's VAPID public key when the org has web
    push on (ADR 0081): the pre-prompt then subscribes this browser, posting
    ``csrf`` (the page's session CSRF token) with the subscription. It needs
    ``live_updates``, since the pre-prompt lives in the stream script; empty
    (the default, and always in local mode) leaves tiers 0-1 exactly as
    described above.
    """
    if dismissible_notice_html and not dismissible_notice_key:
        raise ValueError("wrap(): dismissible_notice_html needs a dismissible_notice_key")
    nonce = nonce or secrets.token_urlsafe(18)
    stream_script = ""
    if live_updates:
        stream_js = _STREAM_JS % {
            "notifications_enabled": "true" if notifications_enabled else "false",
            "notifications_detail": json.dumps(notifications_detail),
            "stream_url": json.dumps(stream_url),
            "push_public_key": json.dumps(push_public_key),
            "push_csrf": json.dumps(csrf if push_public_key else ""),
        }
        stream_script = f'<script nonce="{nonce}">{stream_js}</script>'
    live_html = (
        '<div class="pf-shell-live" role="status" aria-live="polite">'
        '<span class="pf-shell-live-dot" id="pf-shell-live-dot"></span>'
        '<span class="pf-shell-live-label" id="pf-shell-live-label">connecting…</span>'
        "</div>"
    ) if live_updates else ""
    banner = f'<div class="pf-shell-banner" role="alert">{banner_html}</div>' if banner_html else ""
    notice = ""
    if dismissible_notice_html:
        notice = (
            f'<div class="pf-shell-notice" id="pf-shell-notice" '
            f'data-dismiss-key="{_html_escape(dismissible_notice_key)}">'
            f'<span>{dismissible_notice_html}</span>'
            '<button type="button" class="pf-shell-notice-close" data-dismiss-notice '
            'aria-label="Dismiss">&times;</button></div>'
        )
    app_links = _ORG_APP_LINKS if nav_items == ORG_NAV_ITEMS else ""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="{_FAVICON_DATA_URI}">{app_links}
<title>{_html_escape(title)}</title>
<style nonce="{nonce}">{DOCUMENT_CSS}{_SHELL_CSS}</style>
</head>
<body>
{header_html(active, nav_items, live_html=live_html, principal_label=principal_label)}
{banner}
{notice}
<main class="pf-shell-main">{body_html}</main>
<div class="pf-shell-toast" id="pf-shell-toast" role="status"></div>
<div class="pf-sr-only" id="pf-shell-announcer" aria-live="polite"></div>
{stream_script}
</body>
</html>
"""


# The fallback documents: a page with no shell around it because there is nothing to navigate to
# yet (the "not authorized" page) or because it stands in for a card document, which has no shell
# either (the "no longer pending" and "preparing" pages, and local mode's /security). One panel in
# the middle of the page, in the same tokens, components and dark mode as every other document.
_PLAIN_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body.pf-plain {
  min-height: 100vh; background: var(--bg); color: var(--ink); font-family: var(--font-sans);
  font-size: var(--step-body); line-height: 1.6; -webkit-font-smoothing: antialiased;
}
.pf-plain-main { padding-block: var(--space-l) var(--space-xl); }
.pf-plain-panel { max-width: 680px; margin-inline: auto; overflow-wrap: anywhere; }
.pf-plain-panel h1 { font-size: 22px; line-height: 1.25; letter-spacing: -.02em; margin: 0; }
.pf-plain-panel .actions { margin-top: var(--space-m); }
.pf-plain :where(a:not(.button)) { color: var(--accent-dark); }
.pf-plain :where(a:not(.button)):hover { color: var(--accent); }
.pf-plain a.button { text-decoration: none; }
.pf-plain :where(code, pre) { font-family: var(--font-mono); font-size: var(--step-small); }
.pf-plain pre {
  white-space: pre-wrap; background: var(--surface-soft); color: var(--ink);
  border: 1px solid var(--line); border-radius: var(--radius-s); padding: var(--space-xs) var(--space-s);
}
"""


def plain_page(body_html: str, *, title: str, nonce: str, head_html: str = "", page_css: str = "") -> str:
    """A complete document for a page that has no shell: ``body_html`` inside one ``panel``, with
    the viewport meta, the design system and dark mode. ``head_html`` goes into ``<head>`` as is
    (a ``<meta http-equiv="refresh">``, say); ``page_css`` is appended to the one nonce'd
    ``<style>``. ``body_html`` is markup: the caller escapes what it interpolates."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="{_FAVICON_DATA_URI}">
<title>{_html_escape(title)}</title>
{head_html}
<style nonce="{nonce}">{DOCUMENT_CSS}{_PLAIN_CSS}{page_css}</style>
</head>
<body class="pf-plain">
<main class="shell pf-plain-main"><div class="panel stack pf-plain-panel">{body_html}</div></main>
</body>
</html>
"""
