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

_TOKENS_CSS = (Path(__file__).parent / "resources" / "tokens.css").read_text(encoding="utf-8")

# Browser-tab favicon -- the same bundled shield mark approval_icons.py
# already hands the native/web approval cards (resources/icon_32.png, the
# size a favicon is actually rendered at), embedded as a data: URI so this
# shared shell never needs its own unauthenticated route or asset file just
# to satisfy the browser's automatic GET /favicon.ico.
_FAVICON_DATA_URI = approval_icons.icon_data_uri(
    str(Path(__file__).parent / "resources" / "icon_32.png")
)

_SHELL_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--color-bg); color: var(--color-text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", Helvetica, Arial, sans-serif;
  font-size: 14px; min-height: 100vh; display: flex; flex-direction: column;
}
.pf-shell-header {
  display: flex; align-items: center; gap: var(--space-4); flex-wrap: wrap;
  padding: 10px 20px; background: var(--color-surface);
  border-bottom: 1px solid var(--color-divider); flex-shrink: 0;
}
.pf-shell-brand { font-weight: 700; font-size: 14px; letter-spacing: -0.01em; }
.pf-shell-nav { display: flex; gap: 4px; flex: 1; }
.pf-shell-nav-item {
  padding: 5px 10px; border-radius: var(--radius-md); font-size: 13px;
  color: var(--color-text); text-decoration: none; opacity: .7;
}
.pf-shell-nav-item:hover { opacity: 1; background: var(--color-bg); }
.pf-shell-nav-item.active { opacity: 1; font-weight: 600; background: var(--color-bg); }
.pf-shell-live {
  display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--color-neutral-600);
  white-space: nowrap;
}
.pf-shell-live-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--color-neutral-400); flex-shrink: 0;
}
.pf-shell-live-dot.live { background: #2fa84f; }
.pf-shell-live-dot.reconnecting { background: #d9a520; }
.pf-shell-live-dot.down { background: var(--color-danger); }
/* Who this queue belongs to -- org mode only (see wrap's principal_label);
   local mode doesn't render one here, even on a separated install with more
   than one OS-user principal (ADR 0008) -- each one's own page already only
   ever shows their own queue, it just doesn't caption whose it is. */
.pf-shell-principal {
  font-size: 12px; color: var(--color-neutral-700); white-space: nowrap;
  padding-left: 10px; border-left: 1px solid var(--color-divider);
}
.pf-shell-live + .pf-shell-principal { margin-left: 0; }
/* Neither tokens.css nor anything above styles a bare <a>, so any link a
   page renders outside the nav/banner/notice classes lands on the
   browser's default blue -- against a warm grey palette, on pages that
   are otherwise fully tokenized. */
.pf-shell-main a { color: var(--color-accent-700); }
.pf-shell-main a:hover { color: var(--color-accent); }
.pf-shell-banner {
  padding: 8px 20px; font-size: 13px; font-weight: 600; text-align: center;
  background: var(--color-danger); color: #fff; flex-shrink: 0;
}
.pf-shell-banner a { color: #fff; text-decoration: underline; }
.pf-shell-notice {
  padding: 8px 20px; font-size: 13px; text-align: center; flex-shrink: 0;
  background: var(--color-accent-100); color: var(--color-accent-800);
  border-bottom: 1px solid var(--color-divider);
  display: flex; align-items: center; justify-content: center; gap: 10px;
}
.pf-shell-notice a { color: inherit; font-weight: 600; }
.pf-shell-notice-close {
  background: none; border: none; cursor: pointer; font-size: 15px; line-height: 1;
  color: inherit; opacity: .6; padding: 0 2px; flex-shrink: 0;
}
.pf-shell-notice-close:hover { opacity: 1; }
.pf-shell-main { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.pf-shell-toast {
  position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
  background: var(--color-neutral-900); color: var(--color-neutral-100);
  padding: 10px 16px; border-radius: var(--radius-lg); font-size: 13px;
  max-width: min(480px, calc(100vw - 32px)); box-shadow: 0 8px 24px rgba(0,0,0,.25);
  opacity: 0; pointer-events: none; transition: opacity .15s ease;
  z-index: 1000;
}
.pf-shell-toast.shown { opacity: 1; }
.pf-shell-notif-prompt {
  bottom: 72px; display: flex; align-items: center; gap: 10px; pointer-events: auto;
}
.pf-shell-notif-enable {
  background: var(--color-accent); color: #fff; border: none; border-radius: var(--radius-md);
  font-size: 12.5px; font-weight: 600; padding: 5px 10px; cursor: pointer; flex-shrink: 0;
}
.pf-sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
@media (max-width: 480px) {
  .pf-shell-header { padding: 8px 12px; gap: 10px; }
  .pf-shell-brand { display: none; }
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
# Notifications, tiers 0-1 only -- no push, no VAPID, nothing leaving the
# machine (a push tier would be org mode's; see ADR 0064):
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

  if (NOTIFICATIONS_ENABLED && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }

  window.__pfNotifPrompt = function () {
    if (!NOTIFICATIONS_ENABLED) { return; }
    if (!('Notification' in window) || Notification.permission !== 'default') { return; }
    var already;
    try { already = localStorage.getItem('pf_notif_prompted'); } catch (e) { already = null; }
    if (already) { return; }
    try { localStorage.setItem('pf_notif_prompted', '1'); } catch (e) {}
    var bar = document.createElement('div');
    bar.className = 'pf-shell-toast pf-shell-notif-prompt shown';
    bar.setAttribute('role', 'status');
    var text = document.createElement('span');
    text.textContent = 'Want PrivacyFence to notify you when Claude needs approval?';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pf-shell-notif-enable';
    btn.textContent = 'Enable';
    btn.addEventListener('click', function () {
      Notification.requestPermission();
      bar.remove();
    });
    bar.appendChild(text);
    bar.appendChild(btn);
    document.body.appendChild(bar);
    setTimeout(function () { if (bar.parentNode) { bar.remove(); } }, 10000);
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


def _nav_html(active: str, nav_items: tuple[tuple[str, str, str], ...]) -> str:
    items = []
    for key, label, href in nav_items:
        cls = "pf-shell-nav-item active" if key == active else "pf-shell-nav-item"
        items.append(f'<a class="{cls}" href="{href}">{_html_escape(label)}</a>')
    return "".join(items)


def wrap(
    body_html: str, *, title: str, active: str, nonce: str | None = None,
    notifications_enabled: bool = True, notifications_detail: str = "minimal",
    banner_html: str | None = None,
    dismissible_notice_html: str | None = None, dismissible_notice_key: str = "",
    nav_items: tuple[tuple[str, str, str], ...] = _NAV_ITEMS,
    principal_label: str = "",
    live_updates: bool = True,
    stream_url: str = "/api/state/stream",
) -> str:
    """Full ``<!DOCTYPE html>`` document: tokens.css + the shell's own CSS,
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
        }
        stream_script = f'<script nonce="{nonce}">{stream_js}</script>'
    live_html = (
        '<div class="pf-shell-live" role="status" aria-live="polite">'
        '<span class="pf-shell-live-dot" id="pf-shell-live-dot"></span>'
        '<span id="pf-shell-live-label">connecting…</span>'
        "</div>"
    ) if live_updates else ""
    principal_html = (
        f'<div class="pf-shell-principal">{_html_escape(principal_label)}</div>'
        if principal_label else ""
    )
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
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="{_FAVICON_DATA_URI}">
<title>{_html_escape(title)}</title>
<style nonce="{nonce}">{_TOKENS_CSS}{_SHELL_CSS}</style>
</head>
<body>
<header class="pf-shell-header">
<div class="pf-shell-brand">PrivacyFence</div>
<nav class="pf-shell-nav">{_nav_html(active, nav_items)}</nav>
{live_html}{principal_html}
</header>
{banner}
{notice}
<main class="pf-shell-main">{body_html}</main>
<div class="pf-shell-toast" id="pf-shell-toast" role="status"></div>
<div class="pf-sr-only" id="pf-shell-announcer" aria-live="polite"></div>
{stream_script}
</body>
</html>
"""
