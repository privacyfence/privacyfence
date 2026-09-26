"""HTML/CSS/vanilla-JS for the webview settings window (settings_window.py).

Originally transcribed from a Claude Design prototype export (not shipped
with this repo); its look is now the shared design system's (see the note
above ``_CSS``), and only its structure and wording remain. What was never
transcribed is that file's own rendering machinery (a small
declarative-component runtime, ``sc-if``/``sc-for``/``{{ }}`` tags): this
module is plain string templating plus a small amount of vanilla JS driving
the DOM directly, with no framework and no build step -- this is a fully
offline, file:// document (loaded via ``loadHTMLString_baseURL_``), so
nothing here may reference a CDN, a bundler-emitted asset, or the network.

No AppKit/WebKit import here either (see settings_controller.py's own
docstring for why) -- ``test_settings_window_html.py`` asserts on
``build_html()``'s output on any platform, and this module must stay
importable there.

State shape consumed by ``build_html()``/the JS ``render()`` function is
exactly ``SettingsController.snapshot()``'s return value, plus a per-
connector ``icon_data_uri`` field settings_window.py adds before handing the
dict here (icon embedding needs ``approval_window._icon_data_uri()``, which
*is* AppKit/WebKit-tainted -- see that module's docstring -- so it can't
happen in this file).

Bridge protocol (see settings_window.py's module docstring for the Python
side): the page's own ``post(action, payload)`` posts
``window.webkit.messageHandlers.pf.postMessage({action, ...payload})``;
Python answers by calling ``window.__pfRender(newState)`` after handling a
message or finishing a background op. Ephemeral, client-only UI state (which
nav section is active, which privacy group is selected, the Auto-accept
page's own search/filter/add-rule-form state) lives in the JS-side ``ui``
object below and is merged with the Python-pushed state on every render,
using the same field-naming convention the design's own ``Component.state``
established (``section``, ``privacyGroup``, ...) -- never sent to Python.
Text inputs (the Auto-accept page's own value field) commit on blur/Enter, not per keystroke, so a bridge
round-trip mid-typing can't steal focus/cursor position; toggles/segmented
controls/buttons act immediately on click since they're discrete, not free
text. The Auto-accept page's own search/filter inputs are the one exception:
every keystroke re-renders, since filtering that list is itself the whole
point of typing into it, and ``onInput`` below restores focus/cursor
position across that re-render.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from .design_css import DOCUMENT_CSS
from .web.org_settings_scope import LOCAL_MODE, ORG_MODE, NOT_APPLICABLE_ACTIONS

# The settings page is built from the shared design system (resources/design/, ADR 0078/0079),
# which DOCUMENT_CSS inlines ahead of this module's own rules: the tokens (dark mode included --
# app.css redefines the same names under prefers-color-scheme, so nothing below needs a dark
# override of its own), the .shell and .split primitives, and app.css's components (tabstrip/tab,
# card, toggle, field, badge, button). The rules below only arrange them for this page, and use
# no colour literal and no viewport query (tests/unit/test_design_system.py).
#
# Layout. #app is a .shell, named as the pf-settings size container, and every width decision
# below is a container query on it, so the page responds to the room it is given -- a phone, a
# narrow desktop window, the native settings window -- rather than to the viewport. The base
# rules are the narrow layout: the section nav is a tabstrip above the content, Privacy Filter's
# group list a second tabstrip below it, and a settings row stacks its label and description
# above its control. From the website's 900 px nav breakpoint up, the nav becomes a card-styled
# column to the left (.split's two columns) and Privacy Filter's groups a column beside the
# editor.

_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--bg); color: var(--ink); font-family: var(--font-sans); -webkit-font-smoothing: antialiased; }
code { font-family: var(--font-mono); font-size: .92em; overflow-wrap: anywhere; }

#app.pf-settings { container-name: pf-settings; padding-block: var(--space-s) var(--space-xl); }
.split.pf-settings-layout { --split-cols: minmax(0, 1fr); --split-gap: var(--space-s); align-items: start; }
.pf-settings-layout > * { min-width: 0; }

/* ---- Section nav: a tabstrip; a card-styled column from 900 px up ---- */
.pf-nav-version { display: none; padding: 8px 14px 4px; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
@container pf-settings (min-width: 900px) {
  .split.pf-settings-layout { --split-cols: 220px minmax(0, 1fr); --split-gap: var(--space-l); }
  .tabstrip.pf-nav, .tabstrip.pf-subnav {
    flex-direction: column; overflow: visible; padding: 8px; background: var(--surface);
    border-radius: var(--radius-card);
  }
  .pf-nav .tab, .pf-subnav .tab { justify-content: flex-start; white-space: normal; text-align: left; }
  .pf-nav .tab[aria-selected="true"], .pf-subnav .tab[aria-selected="true"] {
    color: var(--accent-dark); background: var(--accent-soft); border-color: var(--accent-line); box-shadow: none;
  }
  .pf-nav-version { display: block; }
}

/* ---- Content ---- */
.pf-content { display: flex; flex-direction: column; gap: var(--space-s); }
.pf-page, .pf-detail-page { display: flex; flex-direction: column; gap: var(--space-s); }
.pf-page > *, .pf-detail-page > * { margin: 0; max-width: 760px; }
.pf-page-title { font-size: clamp(24px, 2vw + 16px, 32px); font-weight: 750; letter-spacing: -.02em; line-height: 1.15; color: var(--ink); }
.pf-page-subtitle { font-size: var(--step-small); color: var(--ink-soft); line-height: 1.55; }
.pf-detail-title { font-size: var(--step-h3); font-weight: 750; color: var(--ink); }
.pf-detail-subtitle { font-size: var(--step-small); color: var(--ink-soft); line-height: 1.55; }
.pf-group-title { font-size: 13px; font-weight: 750; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-top: var(--space-2xs); }
.pf-hint { font-size: var(--step-small); color: var(--muted); line-height: 1.5; }
.pf-empty { font-size: var(--step-small); color: var(--muted); }

/* ---- Banners (error, update, welcome): status cards ---- */
.pf-banner { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-xs); font-size: var(--step-small); line-height: 1.5; }
.pf-banner-dismiss {
  flex: none; display: inline-flex; align-items: center; justify-content: center; min-width: var(--tap); min-height: var(--tap);
  margin: -10px -10px -10px 0; border-radius: var(--radius-s); cursor: pointer; font-weight: 700; color: inherit;
}
.pf-banner-dismiss:hover { background: var(--surface-soft); }
.card-danger .pf-banner-dismiss { color: var(--danger); }

/* ---- A settings row: its label and description, then its control. Stacked by default; side by
   side from 900 px up. ---- */
.pf-card { display: flex; flex-direction: column; gap: var(--space-s); }
.pf-card > h2 { margin: 0; }
.pf-row { display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-xs); }
.pf-row-text { min-width: 0; }
.pf-row-title { font-size: var(--step-body); font-weight: 650; color: var(--ink); }
.pf-row-desc { font-size: var(--step-small); color: var(--ink-soft); margin-top: 4px; line-height: 1.5; }
.pf-row-sub { padding-top: var(--space-s); border-top: 1px solid var(--line); }
.pf-row-sub .pf-row-title { font-size: var(--step-small); font-weight: 500; }
.pf-row-sub[aria-disabled="true"] .pf-row-title { color: var(--muted); }
.pf-row-controls { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-xs); }
@container pf-settings (min-width: 900px) {
  .pf-row { flex-direction: row; align-items: center; justify-content: space-between; gap: var(--space-m); }
  .pf-row-text { flex: 1 1 0; }
  .pf-row > :last-child:not(.pf-row-text) { flex: 0 1 auto; max-width: 50%; }
}

/* ---- Segmented control: tab-shaped options in a soft well; wraps rather than overflows ---- */
.pf-seg-group { display: flex; flex-wrap: wrap; gap: 4px; padding: 4px; background: var(--surface-soft); border: 1px solid var(--line); border-radius: calc(var(--radius-s) + 4px); }
.pf-seg-btn {
  display: inline-flex; align-items: center; justify-content: center; min-height: var(--tap); min-width: var(--tap);
  padding: 0 14px; font-size: var(--step-small); font-weight: 650; color: var(--ink-soft);
  border: 1px solid transparent; border-radius: var(--radius-s); cursor: pointer; white-space: nowrap;
}
.pf-seg-btn:hover { color: var(--ink); background: var(--surface); }
/* The chosen option: a raised surface and a border, not the colour alone; a policy's option also
   takes that policy's status colour, and always names it. */
.pf-seg-btn.plain-active { color: var(--ink); background: var(--surface); border-color: var(--line); box-shadow: var(--shadow-button); }
.pf-seg-btn.policy-allow { color: var(--success); background: var(--success-soft); border-color: currentColor; }
.pf-seg-btn.policy-redact { color: var(--warning); background: var(--warning-soft); border-color: currentColor; }
.pf-seg-btn.policy-block { color: var(--danger); background: var(--danger-soft); border-color: currentColor; }

/* ---- Buttons and links drawn as <div role=button>: the shared .button, and tap-sized links ---- */
.button { text-decoration: none; }
.pf-link, .pf-link-danger {
  display: inline-flex; align-items: center; min-height: var(--tap); padding: 0 4px;
  font-size: var(--step-small); font-weight: 650; color: var(--accent-dark); cursor: pointer; white-space: nowrap;
}
.pf-link:hover { color: var(--accent); }
.pf-link-danger { color: var(--danger); }
.pf-link[aria-disabled="true"] { color: var(--muted); cursor: default; pointer-events: none; }
.button[aria-disabled="true"] { pointer-events: none; }
.pf-input-mono { font-family: var(--font-mono); }

/* ---- Chips that filter or pick: pill toggles, aria-pressed/aria-checked carry the state ---- */
.pf-chips { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2xs); }
.pf-chip {
  display: inline-flex; align-items: center; justify-content: center; min-height: var(--tap); min-width: var(--tap);
  padding: 0 14px; font-size: var(--step-small); font-weight: 650; color: var(--ink-soft); background: var(--surface);
  border: 1px solid var(--line); border-radius: var(--radius-pill); cursor: pointer; white-space: nowrap;
}
.pf-chip:hover { border-color: var(--control-line); color: var(--ink); }
.pf-chip[aria-pressed="true"], .pf-chip[aria-checked="true"] { color: var(--accent-dark); background: var(--accent-soft); border-color: var(--accent-dark); }
.pf-chip[aria-pressed="true"]::before, .pf-chip[aria-checked="true"]::before { content: "\\2713"; margin-right: 6px; }

/* ---- Connectors (local mode) ---- */
.pf-connector-list { display: flex; flex-direction: column; gap: var(--space-xs); }
.pf-connector-row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-xs) var(--space-s); }
.pf-connector-name { display: flex; align-items: center; gap: var(--space-xs); flex: 1 1 200px; min-width: 0; font-weight: 650; }
.pf-connector-icon {
  width: 36px; height: 36px; border-radius: var(--radius-s); background: var(--mark-tile); border: 1px solid var(--line);
  display: flex; align-items: center; justify-content: center; flex: none; overflow: hidden;
}
.pf-connector-icon img { width: 22px; height: 22px; object-fit: contain; }
.pf-connector-actions { display: flex; align-items: center; gap: var(--space-xs); flex-wrap: wrap; }

/* ---- Auto-accept (policy v2) ---- */
.pf-aa-search { flex: 1 1 100%; }
.pf-aa-row-main { display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between; gap: var(--space-xs); }
.pf-aa-sentence { flex: 1 1 260px; min-width: 0; font-size: var(--step-body); color: var(--ink); line-height: 1.5; overflow-wrap: anywhere; }
.pf-aa-verbs { display: flex; flex-wrap: wrap; gap: 4px; }
.pf-aa-links { display: flex; flex-wrap: wrap; align-items: center; gap: 0 var(--space-s); margin-top: 4px; }
.pf-aa-tools { font-size: var(--step-small); color: var(--ink-soft); line-height: 1.5; overflow-wrap: anywhere; }
.pf-aa-usage { font-size: 13px; color: var(--muted); }
.pf-aa-usage-stale { color: var(--warning); font-weight: 650; }
.pf-aa-usage-stale::before { content: "\\26A0\\FE0E  "; }
.pf-aa-row { gap: var(--space-2xs); }

/* ---- Copy-ID toast (right-click a grant row -- see data-copy-id) ---- */
.pf-copy-toast {
  position: fixed; background: var(--ink); color: var(--on-ink); font-size: 12px; font-weight: 600;
  padding: 4px 9px; border-radius: 6px; pointer-events: none; z-index: 999; opacity: .95;
  transform: translate(-50%, -100%);
}

/* ---- Privacy Filter: its group list, then the editor ---- */
.pf-privacy { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space-s); align-items: start; }
.pf-privacy > * { min-width: 0; }
@container pf-settings (min-width: 900px) {
  .pf-privacy { grid-template-columns: 180px minmax(0, 1fr); gap: var(--space-m); }
}
.pf-category-list { padding: 0; }
.pf-category-row { padding: var(--space-s); }
.pf-category-row + .pf-category-row { border-top: 1px solid var(--line); }
.pf-category-label { font-size: var(--step-body); font-weight: 600; color: var(--ink); }
.pf-category-key { font-size: 13px; color: var(--muted); font-family: var(--font-mono); margin-top: 2px; overflow-wrap: anywhere; }
.pf-muted { font-weight: 400; color: var(--muted); }

/* ---- Audit ---- */
.pf-audit-logfile { font-size: 13px; color: var(--ink-soft); font-family: var(--font-mono); overflow-wrap: anywhere; }
.pf-list { padding: 0; overflow: hidden; }
.pf-audit-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px var(--space-xs); padding: 12px var(--space-s); }
.pf-audit-row + .pf-audit-row, .pf-agents-row + .pf-agents-row { border-top: 1px solid var(--line); }
/* Who asked, with its tier -- agent_label.py's wording, the approval list's tiers. On its own
   line when narrow; a fixed column beside the rest from 900 px up. */
.pf-audit-agent { flex: 1 1 100%; display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--ink-soft); min-width: 0; }
/* The name gives way, never the tier marker: a truncated claim must still say it is a claim. */
.pf-audit-agent-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pf-audit-agent-attested .pf-audit-agent-name { color: var(--ink); font-weight: 650; }
.pf-audit-agent-claimed .pf-audit-agent-name, .pf-audit-agent-unknown .pf-audit-agent-name { font-style: italic; }
.pf-audit-tier { flex: none; }
.pf-audit-connector { font-size: 13px; color: var(--muted); flex: none; }
.pf-audit-tool { flex: 1 1 120px; font-size: var(--step-small); color: var(--ink); min-width: 0; overflow-wrap: anywhere; }
.pf-audit-badge { flex: none; }
.pf-audit-time { flex: none; font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums; }
@container pf-settings (min-width: 900px) {
  .pf-audit-agent { flex: 0 0 200px; }
  .pf-audit-connector { width: 80px; }
  .pf-audit-time { width: 70px; text-align: right; }
}
/* The admin's AI-system pin page (ADR 0035 decision 3). */
.pf-agents-row { padding: 12px var(--space-s); display: flex; flex-direction: column; gap: 4px; }
.pf-agents-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px var(--space-xs); }
.pf-agents-name { font-size: var(--step-body); font-weight: 650; color: var(--ink); min-width: 0; overflow-wrap: anywhere; }
.pf-agents-meta { font-size: 13px; color: var(--muted); font-family: var(--font-mono); overflow-wrap: anywhere; }
.pf-agents-controls { display: flex; flex-wrap: wrap; gap: var(--space-2xs); align-items: center; margin-top: 6px; font-size: var(--step-small); color: var(--ink-soft); }

/* ---- About ---- */
.pf-about-page { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 6px; padding-block: var(--space-l); }
.pf-about-icon {
  width: 76px; height: 76px; border-radius: 20px; background: var(--accent); color: var(--on-accent); font-size: 26px;
  font-weight: 750; display: flex; align-items: center; justify-content: center; margin-bottom: var(--space-xs);
}
.pf-about-name { font-size: var(--step-h3); font-weight: 750; color: var(--ink); }
.pf-about-version { font-size: var(--step-small); color: var(--ink-soft); }
.pf-about-desc { font-size: var(--step-small); color: var(--ink-soft); margin-top: var(--space-xs); max-width: 420px; line-height: 1.55; }
.pf-about-license { font-size: 13px; color: var(--muted); }
.pf-about-buttons { display: flex; flex-wrap: wrap; justify-content: center; gap: var(--space-xs); margin-top: var(--space-m); }

/* ---- Telegram sign-in modal ---- */
.pf-modal-overlay {
  position: fixed; inset: 0; background: var(--scrim); display: flex;
  align-items: center; justify-content: center; z-index: 100; padding: var(--gutter);
}
.pf-modal {
  background: var(--surface); color: var(--ink); border: 1px solid var(--line); border-radius: var(--radius-card);
  padding: var(--space-m); width: min(400px, 100%); box-shadow: var(--shadow); display: flex; flex-direction: column; gap: var(--space-xs);
}
.pf-modal-title { font-size: var(--step-h3); font-weight: 750; }
.pf-modal-desc { font-size: var(--step-small); color: var(--ink-soft); line-height: 1.5; }
.pf-modal-error { font-size: var(--step-small); color: var(--danger); font-weight: 600; }
.pf-modal-buttons { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--space-xs); margin-top: var(--space-xs); }
"""

# ---------------------------------------------------------------------------- #
# JS -- render(state) rebuilds #app's innerHTML from the merged python+ui
# state on every call; post() is the JS->Python half of the bridge.
# ---------------------------------------------------------------------------- #

_JS = r"""
(function () {
  // window.__pfCapabilities is Python's own settings_window_html.
  // build_html()/_capabilities_for() -- which inner-nav sections this
  // (mode, is_admin) combination gets, and which individual actions never
  // have a real route to post to at all (org_settings_scope.
  // NOT_APPLICABLE_ACTIONS, plus the handful of local-only bespoke actions
  // that table doesn't cover -- see that function's own docstring). Every
  // call site that sets no capabilities renders under the fallback below, which hides
  // nothing -- local mode's own rendering is unaffected by any of this
  // (ADR 0032).
  var CAPS = window.__pfCapabilities || {
    mode: 'local', is_admin: false,
    sections: { general: true, connectors: true, auto_accept: true, privacy: true, audit: true, agents: false, about: true },
    not_applicable_actions: [],
  };

  function notApplicable(action) {
    return CAPS.not_applicable_actions.indexOf(action) !== -1;
  }

  var ui = {
    // window.__pfInitialSection lets one specific route
    // (GET /settings/connectors, see web/routes_settings.py) land here with
    // Connectors already selected -- a real, server-decided initial value
    // for what's otherwise purely client-side UI state (see this module's
    // own docstring on `ui`). Every other route omits the script that sets
    // it, so this falls back to 'general' exactly as before -- except in a
    // mode where General itself is hidden (a non-admin org
    // principal), where landing on a nav item that isn't drawn at all would
    // leave the page with no visible selection; Auto-accept is the one
    // section every org principal, admin or not, always gets.
    section: (window.__pfInitialSection || (CAPS.sections.general ? 'general' : 'auto_accept')),
    privacyGroup: null,
    // Auto-accept page -- see renderAutoAccept below for how each is used.
    aaSearch: '', aaConnectorFilter: [], aaFamilyFilter: [], aaExpanded: {},
    aaGroup: null, aaValue: '', aaCheckedVerbs: {},
    // Dismissible client-side only (never sent to Python, same reasoning
    // as every other `ui.*` field) -- see renderWelcomeBanner below.
    welcomeBannerDismissed: false,
    telegramModalOpen: false,
    // Tracks whether we've actually observed a non-null telegram_auth.step
    // from Python yet -- a fresh, never-submitted modal also has step ===
    // null, and without this the auto-close-on-success check in render()
    // couldn't tell "flow just succeeded" apart from "flow never started".
    telegramAuthWasActive: false,
  };
  var pyState = null;

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function post(action, payload) {
    var msg = Object.assign({ action: action }, payload || {});
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.pf) {
      window.webkit.messageHandlers.pf.postMessage(msg);
    }
  }

  function dataAttr(action, payload) {
    return 'data-action="' + esc(action) + '" data-payload=\'' + JSON.stringify(payload || {}).replace(/'/g, '&#39;') + '\'';
  }

  function toggleHtml(on, action, payload, disabled, ariaLabel) {
    // role="switch"/aria-checked (not role="button") -- this is a genuine
    // binary on/off control, and a QA/AT script needs a real checked state
    // to read back, not just a clickable target. See the PR report for
    // exactly which System-Events AX path reads this in practice.
    //
    // app.css's toggle component: a real checkbox (so it is focusable and
    // announced, and Space flips it) inside a <label> that is the 44 px tap
    // target. The data-action sits on the input, so a tap on the label is
    // one click on the input, one post. onClick cancels the browser's own
    // flip: the switch shows what Python's next render says, as before.
    var attrs = disabled ? ' disabled' : ' ' + dataAttr(action, payload);
    return '<label class="toggle pf-toggle"><input type="checkbox" role="switch"' + (on ? ' checked' : '') +
      ' aria-checked="' + (on ? 'true' : 'false') + '"' +
      (ariaLabel ? ' aria-label="' + esc(ariaLabel) + '"' : '') + attrs +
      '><span class="toggle-track"></span></label>';
  }

  // Right-click-to-copy for a grant row's resource ID (data-copy-id, see
  // renderRules) -- document.execCommand('copy') rather than
  // navigator.clipboard.writeText, since this is an offline file:// document
  // (loadHTMLString_baseURL_, see this module's docstring) and the async
  // Clipboard API is only available in a secure context; the legacy
  // execCommand path has no such restriction and still works from a
  // contextmenu event's user activation.
  function copyToClipboard(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    try { document.execCommand('copy'); } catch (err) { /* best-effort */ }
    document.body.removeChild(ta);
  }

  function showCopyToast(x, y, text) {
    var toast = document.createElement('div');
    toast.className = 'pf-copy-toast';
    toast.textContent = text;
    toast.style.left = x + 'px';
    toast.style.top = (y - 8) + 'px';
    document.body.appendChild(toast);
    setTimeout(function () { toast.remove(); }, 900);
  }

  function segGroupHtml(items, groupLabel) {
    // items: [{label, active, action, payload, colorClass}]
    // role="radiogroup"/"radio" -- a segmented control is a mutually
    // exclusive choice among named options, the same semantics as a radio
    // group, not a set of independent buttons.
    var html = '<div class="pf-seg-group" role="radiogroup"' + (groupLabel ? ' aria-label="' + esc(groupLabel) + '"' : '') + '>';
    items.forEach(function (it) {
      var cls = 'pf-seg-btn' + (it.active ? ' ' + (it.colorClass || 'plain-active') : '');
      var optionLabel = groupLabel ? groupLabel + ': ' + it.label : it.label;
      html += '<div class="' + cls + '" role="radio" aria-checked="' + (it.active ? 'true' : 'false') +
        '" tabindex="0" aria-label="' + esc(optionLabel) + '" ' + dataAttr(it.action, it.payload) + '>' + esc(it.label) + '</div>';
    });
    html += '</div>';
    return html;
  }

  // One settings row: its title and description, then its control
  // (_CSS's .pf-row: stacked when the page is narrow, side by side from
  // 900 px up). `title` is text; `descHtml` and `controlHtml` are markup the
  // caller has already escaped. A `dimmed` row is one whose parent switch is
  // off: it says so to assistive tech as well as by its colour.
  function rowHtml(title, descHtml, controlHtml, cls, dimmed) {
    return '<div class="pf-row' + (cls ? ' ' + cls : '') + '"' + (dimmed ? ' aria-disabled="true"' : '') + '>' +
      '<div class="pf-row-text"><div class="pf-row-title">' + esc(title) + '</div>' +
      (descHtml ? '<div class="pf-row-desc">' + descHtml + '</div>' : '') + '</div>' +
      (controlHtml || '') + '</div>';
  }

  // -------------------------------------------------------------------- //
  // Nav
  // -------------------------------------------------------------------- //

  var NAV_ITEMS = [
    ['general', 'General'], ['connectors', 'Connectors'], ['auto_accept', 'Auto-accept'],
    ['privacy', 'Privacy Filter'], ['audit', 'Audit Log'], ['agents', 'AI systems'], ['about', 'About'],
  ];

  // A tabstrip (app.css) above the content when the page is narrow, a
  // card-styled column beside it from 900 px up (_CSS's container queries).
  function renderNav(state) {
    var html = '<div class="tabstrip pf-nav" role="tablist" aria-label="Settings sections">';
    NAV_ITEMS.forEach(function (item) {
      var key = item[0], label = item[1];
      if (CAPS.sections[key] === false) { return; }
      var active = ui.section === key;
      html += '<div class="tab pf-navitem' + (active ? ' active' : '') + '" role="tab" aria-selected="' +
        (active ? 'true' : 'false') + '" tabindex="0" aria-label="' + esc(label) + '" data-nav="' + key + '">' + esc(label) + '</div>';
    });
    html += '<div class="pf-nav-version">PrivacyFence ' + esc(state.about.version) + '</div>';
    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // General
  // -------------------------------------------------------------------- //

  // Approval-notification permission (the tier-1 notifications of ADR 0064) is
  // browser state, not anything SettingsController tracks or this window's
  // Python side could toggle -- Notification.permission lives per-origin in
  // the browser itself, so this reads it live off `window` at render time
  // rather than off `state`. Deliberately independent of window.
  // __pfNotifPrompt's own one-shot toast (web_shell.py): that toast fires
  // once, right after a first decision, and never again once shown; this
  // card is the permanent, re-visitable home for the same action -- exactly
  // what the toast alone can't be: a toggle that, if permission is
  // denied, says so and links to the browser's own instructions. Granting (or denying) here also means the toast simply
  // won't fire later: its own guard is `Notification.permission ===
  // 'default'`, which this card's own Enable click has already moved past.
  //
  // window.__pfNotificationsEnabled (set by web_shell.py's own script,
  // which runs before this one's DOMContentLoaded-deferred first render --
  // see that module's docstring) is undefined in the one place this
  // function's shared JS also runs without that script at all: the native
  // settings window (loadHTMLString_baseURL_(html, None), no origin at
  // all). `Notification` is typically unsupported there too for the same
  // reason, so the feature-detect below already hides this card on native
  // in the common case; the config-disabled branch only ever applies to
  // the web surface, where that flag is always set.
  //
  // web.notifications.detail (settings.yaml.example; how much a notification
  // body may say, ADR 0064) -- unlike the enabled flag above, this one
  // *is* a real control: set_notifications_detail persists straight to
  // config (settings_controller.py) and the returned snapshot's own
  // general.notifications_detail is what drives this segmented control's
  // active option, so a click here needs no browser permission at all and
  // no page reload to take -- the response to that one POST already
  // carries the new state.general the shared render() dispatch re-renders
  // from (see settings_window_html.py's own bridge protocol docstring).
  var NOTIFICATIONS_DETAIL_LABELS = {
    minimal: 'Minimal', standard: 'Standard', detailed: 'Detailed',
  };
  var NOTIFICATIONS_DETAIL_DESCRIPTIONS = {
    minimal: 'Just the pending count -- "1 approval pending".',
    standard: 'Adds connector, tool, and read/write direction.',
    detailed: 'Also adds the approval\'s own summary line -- the one level that can put gated content on a lock screen.',
  };

  function renderNotificationsDetailControl(state) {
    // A second row inside the notifications card, divided from the first --
    // the same shape renderAudit's own "Log level" row uses below.
    var current = state.general.notifications_detail || 'minimal';
    return rowHtml('Detail level', esc(NOTIFICATIONS_DETAIL_DESCRIPTIONS[current] || ''),
      segGroupHtml(['minimal', 'standard', 'detailed'].map(function (lvl) {
        return { label: NOTIFICATIONS_DETAIL_LABELS[lvl], active: current === lvl, action: 'set_notifications_detail', payload: { level: lvl } };
      }), 'Notification detail level'), 'pf-row-sub');
  }

  function renderNotificationsCard(state) {
    var control;
    if (typeof Notification === 'undefined') {
      control = '<div class="pf-hint">Not supported in this window.</div>';
    } else if (window.__pfNotificationsEnabled === false) {
      control = '<div class="pf-hint">Turned off in configuration (web.notifications.enabled).</div>';
    } else if (Notification.permission === 'granted') {
      control = '<span class="badge badge-success">Enabled</span>';
    } else if (Notification.permission === 'denied') {
      control = '<div class="pf-hint">Blocked -- allow notifications for this site in your browser\'s settings, then reload.</div>';
    } else {
      control = '<div class="button primary" role="button" tabindex="0" aria-label="Enable notifications" data-notif-enable="1">Enable</div>';
    }
    var html = '<div class="card pf-card">';
    html += rowHtml('Approval Notifications',
      'A desktop notification when Claude needs your approval and this tab isn\'t focused -- from your browser, no server or push service involved.',
      control);
    // The detail-level control is independent of Notification.permission
    // (it's a config value, not a browser grant) -- shown whenever this
    // surface could act on it at all, i.e. whenever the card itself isn't
    // hidden or config-disabled above.
    if (typeof Notification !== 'undefined' && window.__pfNotificationsEnabled !== false && !notApplicable('set_notifications_detail')) {
      html += renderNotificationsDetailControl(state);
    }
    html += '</div>';
    return html;
  }

  function renderGeneral(state) {
    var g = state.general;
    var html = '<div class="pf-page">';
    html += '<h1 class="pf-page-title">General</h1>';
    html += renderNotificationsCard(state);

    // The update-available notice is an in-page banner whose three
    // buttons map onto three outcomes (skip this version / remind me
    // later / download), rather than a blocking native modal an
    // HTTP request has no business popping up on the daemon's machine.
    if (g.update_available) {
      html += '<div class="card card-info pf-card pf-update-banner">';
      html += rowHtml('Update available', 'PrivacyFence ' + esc(g.update_latest_version) +
        (g.update_is_beta ? ' (beta)' : '') + ' is available (you have ' + esc(g.version) + ').',
        '<div class="pf-row-controls">' +
        '<a class="button primary" href="' + esc(g.update_release_url || '') + '" target="_blank" rel="noopener">Download</a>' +
        '<div class="button secondary" role="button" tabindex="0" aria-label="Remind me later" ' +
        dataAttr('remind_later_update', {}) + '>Remind Me Later</div>' +
        '<div class="button secondary" role="button" tabindex="0" aria-label="Skip this version" ' +
        dataAttr('skip_update', {}) + '>Skip</div></div>');
      html += '</div>';
    }

    html += '<div class="card pf-card">';
    html += rowHtml('PII Detection Gate',
      'Scans review-popup content for likely personal data (IBANs, national IDs, financial figures) before you approve it. A match requires a second confirmation.',
      toggleHtml(g.pii_enabled, 'toggle_pii_detection', {}, false, 'PII Detection Gate'));
    html += rowHtml('Detect IP addresses', '',
      toggleHtml(g.pii_ip, 'toggle_pii_category', { category_key: 'detect_ip_addresses' }, !g.pii_enabled, 'Detect IP addresses'),
      'pf-row-sub', !g.pii_enabled);
    html += rowHtml('Detect financial figures', '',
      toggleHtml(g.pii_financial, 'toggle_pii_category', { category_key: 'detect_financial_figures' }, !g.pii_enabled, 'Detect financial figures'),
      'pf-row-sub', !g.pii_enabled);
    html += '</div>';

    // A plain same-origin <a>, not a data-action AJAX call --
    // /security is its own standalone page (web/routes_security.py), not
    // part of this SPA's own render() dispatch.
    html += '<div class="card pf-card">';
    html += rowHtml('Security', 'Manage passkeys (Face ID, Touch ID, Windows Hello) enrolled against this install.',
      '<a class="button secondary" href="/security">Manage passkeys</a>');
    // The "turn step-up on" control -- one-directional (see
    // SettingsController.enable_step_up's own docstring for why turning
    // it back off stays a config.yaml-plus-restart operation with no
    // control here). Three states, mirroring what enable_step_up itself
    // will and won't accept: already on (nothing left to do -- a hand
    // edit is the only way back to any other state), on but no passkey
    // yet (the control would just 400/self.error -- hidden, with a hint
    // pointing at the button above instead of a control guaranteed to
    // fail), or ready (the actual button). See ADR 0068.
    // enable_step_up hardcodes LOCAL_PRINCIPAL throughout
    // (org_settings_scope.ACTION_SCOPES's own comment on enable_step_up) --
    // stepUpApplicable is g.step_up_available further gated on this not
    // being one of the modes/principals that control could never act
    // correctly for.
    var stepUpApplicable = g.step_up_available && !notApplicable('enable_step_up');
    if (stepUpApplicable && g.step_up_on) {
      html += rowHtml('Step-up for approvals', 'On -- a write approval or a sensitive settings change demands your passkey. ' +
        'To turn this off, edit <code>step_up.require_passkey</code> in <code>config/settings.yaml</code> and restart PrivacyFence.',
        '', 'pf-row-sub');
    } else if (stepUpApplicable && !g.step_up_has_passkey) {
      html += rowHtml('Step-up for approvals', 'Off. Add a passkey above first, then come back here to require it for every write approval.',
        '', 'pf-row-sub');
    } else if (stepUpApplicable) {
      html += rowHtml('Step-up for approvals', 'Off. Require your passkey for every write approval and every sensitive settings change.',
        '<div class="button primary" role="button" tabindex="0" aria-label="Turn on step-up for approvals" ' +
        dataAttr('enable_step_up', {}) + '>Turn on</div>', 'pf-row-sub');
    }
    html += '</div>';

    // Both cards below are meaningless on a headless org server --
    // an update check against GitHub Releases and installing *this org's
    // own* configuration bundle are both local-desktop-install concepts
    // (org_settings_scope.NOT_APPLICABLE_ACTIONS covers toggle_update_check;
    // install_org_config has no ACTION_SCOPES entry at all, since it isn't
    // one of the generic dispatcher's actions -- both are the same "hidden
    // for org" decision).
    if (!notApplicable('toggle_update_check')) {
      html += '<div class="card pf-card">';
      html += rowHtml('Check for Updates', 'Once-a-day check against GitHub Releases. Never installs anything automatically.',
        toggleHtml(g.update_check_enabled, 'toggle_update_check', {}, false, 'Check for Updates'));
      html += rowHtml('Receive beta releases', '',
        toggleHtml(g.update_check_beta, 'toggle_update_check_beta', {}, !g.update_check_enabled, 'Receive beta releases'),
        'pf-row-sub', !g.update_check_enabled);
      html += '</div>';
    }

    if (!notApplicable('install_org_config')) {
      var installed = g.org_installed && g.org_installed_date
        ? '<div class="pf-hint">Installed ' + esc(g.org_installed_date) + '</div>'
        : (!g.org_installed ? '<span class="badge">Not installed</span>' : '');
      html += '<div class="card pf-card">';
      html += rowHtml('Organization Configuration', 'OAuth app credentials and unattended-session policy, provided by your IT administrator.',
        '<div class="pf-row-controls"><div class="button primary" role="button" tabindex="0" aria-label="' + esc(g.org_button_label) + '" ' +
        dataAttr('install_org_config', {}) + '>' + esc(g.org_button_label) + '</div>' + installed + '</div>');
      html += '</div>';
    }

    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Connectors
  // -------------------------------------------------------------------- //

  function connectorStatus(c) {
    // app.css badges: the text always names the state, the colour repeats it.
    if (c.busy) return { text: 'Connecting…', cls: 'badge-warning' };
    if (c.authed) return { text: 'Connected', cls: 'badge-success' };
    if (!c.enabled) return { text: 'Disabled', cls: 'badge-dashed' };
    if (!c.has_org) {
      return { text: c.key === 'telegram' ? 'App credentials missing' : 'Organization config missing', cls: 'badge-danger' };
    }
    return { text: 'Not connected', cls: '' };
  }

  // Shown on first run (nothing authenticated yet) so
  // landing here from the companion's Open Settings item isn't a blank
  // connector list with no explanation of what any of it means or what
  // order to do things in. Dismissible, client-side only -- see ui.
  // welcomeBannerDismissed's own comment above; a page reload brings it
  // back until a connector is actually authenticated, at which point
  // `anyAuthed` below stops it from rendering at all.
  function renderWelcomeBanner(state) {
    if (ui.welcomeBannerDismissed) return '';
    var anyAuthed = state.connectors.some(function (c) { return c.authed; });
    if (anyAuthed) return '';
    var html = '<div class="card card-info pf-welcome-banner"><div class="pf-banner">';
    html += '<div><div class="pf-row-title">Welcome to PrivacyFence</div>';
    html += '<div class="pf-row-desc">PrivacyFence is a privacy and approval gateway between Claude and your ' +
      'real accounts (Gmail, Drive, Slack, and similar) -- it governs access to them, it does not provide them ' +
      'itself. Nothing is governed until at least one connector below is authenticated. If your IT team gave ' +
      'you an organization config bundle, install it first from the General page; otherwise authenticate a ' +
      'connector directly below, then go back to Claude.</div></div>';
    html += '<div class="pf-banner-dismiss" role="button" tabindex="0" aria-label="Dismiss welcome message" ' +
      'data-dismiss-welcome="1">✕</div>';
    html += '</div></div>';
    return html;
  }

  function renderConnectors(state) {
    var html = '<div class="pf-page">';
    html += '<h1 class="pf-page-title">Connectors</h1>';
    html += '<div class="pf-page-subtitle">Authenticate a connector to let Claude access it, subject to approval and policy. ' +
      'Signing in opens a browser window on the machine running PrivacyFence -- not necessarily this device.</div>';
    html += renderWelcomeBanner(state);
    html += '<div class="pf-connector-list">';
    state.connectors.forEach(function (c) {
      var status = connectorStatus(c);
      html += '<div class="card pf-connector-row">';
      html += '<div class="pf-connector-name"><div class="pf-connector-icon">' +
        (c.icon_data_uri ? '<img src="' + esc(c.icon_data_uri) + '" alt="" width="22" height="22"/>' : '') + '</div>';
      html += '<span>' + esc(c.label) + '</span><span class="badge ' + status.cls + '">' + esc(status.text) + '</span></div>';
      html += '<div class="pf-connector-actions">';
      var authDisabled = c.busy;
      if (c.key === 'telegram') {
        // Telegram's phone/code/2FA flow needs its own multi-step modal
        // (see renderTelegramModal below) instead of the generic single-
        // click OAuth flow every other connector uses -- intercepted here
        // client-side (data-telegram-auth, not data-action) so opening the
        // modal at the phone-entry step needs no round trip to Python;
        // the first real bridge call is telegram_start_auth() once a
        // phone number is actually submitted.
        html += '<div class="pf-link pf-auth-link" role="button" tabindex="0" aria-label="' + esc(c.auth_label) + ' Telegram"' +
          (authDisabled ? ' aria-disabled="true"' : ' data-telegram-auth="1"') + '>' + esc(c.auth_label) + '</div>';
      } else {
        html += '<div class="pf-link pf-auth-link" role="button" tabindex="0" aria-label="' + esc(c.auth_label) + ' ' + esc(c.label) + '" ' +
          (authDisabled ? 'aria-disabled="true"' : dataAttr('authenticate_connector', { connector: c.key })) + '>' + esc(c.auth_label) + '</div>';
      }
      // Directional, not a single
      // toggle_connector -- re-enabling a connector is gated
      // (_SENSITIVE_ACTIONS) differently from disabling one (see
      // web/routes_settings.py's own classification comment), so the
      // action this click sends has to match which direction the very
      // next click will actually take (ADR 0070).
      html += toggleHtml(c.enabled, c.enabled ? 'disable_connector' : 'enable_connector', { connector: c.key }, false, c.label + ' enabled');
      html += '</div></div>';
    });
    html += '</div></div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Auto-accept (policy v2). One filterable rule list, sentence-rendered
  // server-side (policy.describe). Every "Add rule" submission writes straight to the v2
  // auto_accept: section (settings_controller.add_policy_rule) -- there is
  // no rule_type dropdown here the way the old per-operation rows had one,
  // because a scope's own verb checkboxes (aa.scope_groups[].verbs) are
  // what a v2 rule is actually keyed on, not a v1 rule name.
  //
  // Each row carries its own usage line (settings_controller._auto_accept_state, from
  // AuditLogger.rule_usage()) -- "Matched Nx, last <when>" for a rule that has actually let
  // something through, or a distinct stale badge next to the Remove link for one that never
  // has, answering "which of my rules have never matched".
  // -------------------------------------------------------------------- //

  // A verb's family as a badge: read is the accent, send a warning,
  // destructive a danger; the verb's own name is always the text.
  var VERB_BADGE = { read: 'badge-accent', write: '', send: 'badge-warning', destructive: 'badge-danger' };

  function verbChipsHtml(verbs) {
    return verbs.map(function (v) {
      return '<span class="badge ' + (VERB_BADGE[v.family] || '') + ' pf-verb-chip">' + esc(v.verb) + '</span>';
    }).join('');
  }

  function renderAutoAccept(state) {
    var aa = state.auto_accept;
    var search = (ui.aaSearch || '').trim().toLowerCase();
    var connFilter = ui.aaConnectorFilter || [];
    var famFilter = ui.aaFamilyFilter || [];
    var expanded = ui.aaExpanded || {};

    var html = '<div class="pf-page">';
    html += '<h1 class="pf-page-title">Auto-accept</h1>';
    html += '<div class="pf-page-subtitle">Every standing rule that lets Claude act without asking first, across every connector -- what a rule actually unblocks is shown before you add it, and again on its own row below.</div>';

    html += '<div class="pf-chips pf-aa-filterbar">';
    html += '<input type="text" class="field pf-aa-search" placeholder="Filter by connector, tool, or value…" aria-label="Filter rules" value="' +
      esc(ui.aaSearch) + '" data-aa-search="1"/>';
    aa.connectors.forEach(function (cname) {
      var active = connFilter.indexOf(cname) !== -1;
      html += '<div class="pf-chip' + (active ? ' active' : '') + '" role="button" tabindex="0" aria-pressed="' +
        (active ? 'true' : 'false') + '" aria-label="Filter to ' + esc(cname) + '" data-aa-connector-filter="' +
        esc(cname) + '">' + esc(cname) + '</div>';
    });
    ['read', 'write', 'send', 'destructive'].forEach(function (fam) {
      var active = famFilter.indexOf(fam) !== -1;
      html += '<div class="pf-chip' + (active ? ' active' : '') + '" role="button" tabindex="0" aria-pressed="' +
        (active ? 'true' : 'false') + '" aria-label="Filter to ' + esc(fam) + ' rules" data-aa-family-filter="' +
        fam + '">' + esc(fam) + '</div>';
    });
    html += '</div>';

    var rows = aa.rules.filter(function (r) {
      if (connFilter.length && connFilter.indexOf(r.connector) === -1) return false;
      if (famFilter.length && !r.verbs.some(function (v) { return famFilter.indexOf(v.family) !== -1; })) return false;
      if (!search) return true;
      var haystack = (r.sentence + ' ' + r.connector + ' ' + r.covered_tools.join(' ') + ' ' + r.value_ids.join(' ')).toLowerCase();
      return haystack.indexOf(search) !== -1;
    });

    if (rows.length === 0) {
      html += '<div class="pf-empty pf-rules-empty">' +
        (aa.rules.length ? 'No rules match this filter.' : 'No auto-accept rules configured yet -- add one below.') +
        '</div>';
    }
    rows.forEach(function (r) {
      var isExpanded = !!expanded[r.id];
      var copyAttr = r.value_ids.length ? ' data-copy-id="' + esc(r.value_ids.join(', ')) +
        '" title="' + esc(r.value_ids.join(', ')) + ' -- right-click to copy"' : '';
      html += '<div class="card pf-card pf-aa-row"' + copyAttr + '>';
      html += '<div class="pf-aa-row-main"><div class="pf-aa-sentence">' + esc(r.sentence) + '</div>';
      html += '<div class="pf-aa-verbs">' + verbChipsHtml(r.verbs) + '</div></div>';
      html += '<div class="pf-aa-links">';
      html += '<div class="pf-link" role="button" tabindex="0" aria-expanded="' + (isExpanded ? 'true' : 'false') +
        '" aria-label="What this unblocks" data-aa-expand="' + esc(r.id) + '">' + (isExpanded ? '▾' : '▸') +
        ' Unblocks ' + r.covered_tools.length + ' tool' + (r.covered_tools.length === 1 ? '' : 's') + '</div>';
      html += '<div class="pf-link-danger" role="button" tabindex="0" aria-label="Remove rule" ' +
        dataAttr('remove_policy_rule', { rule_id: r.id }) + '>✕ Remove</div>';
      html += '</div>';
      // Per-rule usage -- "Matched 42x, last 3 days ago" or, for a rule that has never
      // fired, a distinct stale badge nudging toward the Remove link right above it.
      html += '<div class="pf-aa-usage' + (r.never_matched ? ' pf-aa-usage-stale' : '') + '">' +
        (r.never_matched
          ? 'Never matched -- consider removing it above.'
          : 'Matched ' + r.match_count + 'x' + (r.last_matched ? ', last ' + esc(r.last_matched) : '')) +
        '</div>';
      if (isExpanded) {
        html += '<div class="pf-aa-tools">' + esc(r.covered_tools.join(', ')) + '</div>';
      }
      html += '</div>';
    });

    if (!ui.aaGroup && aa.scope_groups.length) ui.aaGroup = aa.scope_groups[0].id;
    var group = null;
    aa.scope_groups.forEach(function (g) { if (g.id === ui.aaGroup) group = g; });
    var checkedVerbs = ui.aaCheckedVerbs || {};

    html += '<div class="card pf-card pf-aa-add"><h2 class="pf-row-title">Add a rule</h2>';
    html += '<label><span class="field-label">Scope</span><select class="field" data-aa-group-select="1">';
    aa.scope_groups.forEach(function (g) {
      html += '<option value="' + esc(g.id) + '"' + (g.id === ui.aaGroup ? ' selected' : '') + '>' + esc(g.label) + '</option>';
    });
    html += '</select></label>';
    if (group && group.needs_value) {
      html += '<input type="text" class="field pf-input-mono" placeholder="' + esc(group.value_hint) +
        '" aria-label="Value (comma-separated for more than one)" value="' + esc(ui.aaValue) + '" data-aa-value="1"/>';
    }
    if (group) {
      html += '<div class="pf-chips pf-caps-row">';
      group.verbs.forEach(function (verb) {
        var on = !!checkedVerbs[verb];
        html += '<div class="pf-chip' + (on ? ' on' : '') + '" role="checkbox" aria-checked="' + (on ? 'true' : 'false') +
          '" tabindex="0" aria-label="' + esc(verb) + '" data-aa-verb-toggle="' + esc(verb) + '">' + esc(verb) + '</div>';
      });
      html += '</div>';
    }
    html += '<div><div class="button primary" role="button" tabindex="0" aria-label="Add rule" data-aa-add="1">Add rule</div></div>';
    html += '</div>';

    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Privacy
  // -------------------------------------------------------------------- //

  var POLICY_COLOR_CLASS = { allow: 'policy-allow', redact: 'policy-redact', block: 'policy-block' };

  function policySegHtml(current, onAction, basePayload, groupLabel) {
    return segGroupHtml(['allow', 'redact', 'block'].map(function (p) {
      var payload = Object.assign({}, basePayload, { policy: p });
      return { label: p.charAt(0).toUpperCase() + p.slice(1), active: current === p, action: onAction, payload: payload, colorClass: POLICY_COLOR_CLASS[p] };
    }), groupLabel);
  }

  function renderPrivacy(state) {
    var privacy = state.privacy;
    if (!ui.privacyGroup && privacy.groups.length) ui.privacyGroup = privacy.groups[0].key;

    // The group list is a second tabstrip under the section nav when the
    // page is narrow, and a column beside the editor from 900 px up.
    var html = '<div class="pf-privacy">';
    html += '<div class="tabstrip pf-subnav" role="tablist" aria-label="Privacy Filter group">';
    privacy.groups.forEach(function (pg) {
      var active = ui.privacyGroup === pg.key;
      html += '<div class="tab pf-subnav-item' + (active ? ' active' : '') + '" role="tab" aria-selected="' +
        (active ? 'true' : 'false') + '" tabindex="0" aria-label="' + esc(pg.label) +
        '" data-privacy-nav="' + esc(pg.key) + '"><span>' + esc(pg.label) + '</span></div>';
    });
    html += '</div>';

    html += '<div class="pf-detail-page">';
    if (ui.privacyGroup === 'calendar') {
      html += '<h1 class="pf-detail-title">Calendar</h1>';
      html += '<div class="pf-detail-subtitle">Calendar has no category schema — this is its one privacy-relevant setting.</div>';
      html += '<div class="card pf-card">' + rowHtml('Show full event details in free/busy',
        'When off, calendar_get_free_busy always returns busy/free blocks only, never titles or status, regardless of access.',
        toggleHtml(privacy.calendar_free_busy, 'toggle_calendar_free_busy', {}, false, 'Show full event details in free/busy')) + '</div>';
    } else {
      var group = ui.privacyGroup;
      var label = '';
      privacy.groups.forEach(function (pg) { if (pg.key === group) label = pg.label; });
      var defaultPolicy = privacy.default_policy[group];
      var categories = privacy.categories[group] || [];

      html += '<h1 class="pf-detail-title">' + esc(label) + '</h1>';
      html += '<div class="pf-detail-subtitle">Applied before this data reaches the review popup, Claude, or the audit log — a floor under human review, not a substitute for it.</div>';

      html += '<div class="card pf-card pf-policy-row">' + rowHtml('Default policy', 'For categories not listed below.',
        policySegHtml(defaultPolicy, 'set_default_policy', { group: group }, 'Default policy')) + '</div>';

      if (categories.length) {
        html += '<div class="card pf-list pf-category-list">';
        categories.forEach(function (cat) {
          html += '<div class="pf-row pf-category-row"><div class="pf-row-text"><div class="pf-category-label">' + esc(cat.label) + '</div>';
          html += '<div class="pf-category-key">' + esc(cat.key) + '</div></div>';
          html += policySegHtml(cat.policy, 'set_category_policy', { group: group, category: cat.key }, cat.label + ' policy');
          html += '</div>';
        });
        html += '</div>';
      }

      if (group === 'privacy' && typeof privacy.gmail_append_signature === 'boolean') {
        html += '<div class="card pf-card">' + rowHtml('Append Gmail signature to drafts',
          'Adds your Gmail signature to the end of every draft Claude creates, unless a call says otherwise. It is shown in the approval popup with the rest of the draft.',
          toggleHtml(privacy.gmail_append_signature, 'toggle_gmail_signature', {}, false, 'Append Gmail signature to drafts')) + '</div>';
      }
    }
    html += '</div></div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Audit
  // -------------------------------------------------------------------- //

  var LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];
  // The decision is the badge's text; its colour repeats it.
  var AUDIT_BADGE = { denied: 'badge-danger', auto_accepted: 'badge-accent', other: '' };

  function renderAudit(state) {
    var audit = state.audit;
    var html = '<div class="pf-page">';
    html += '<h1 class="pf-page-title">Audit Log</h1>';
    html += notApplicable('export_audit_log')
      ? '<div class="pf-page-subtitle">Your own recent decisions — accepted, denied, or auto-accepted — and which AI system asked for each.</div>'
      : '<div class="pf-page-subtitle">Every decision — accepted, denied, or auto-accepted — is recorded locally as JSON lines, then exported weekly to a formatted Excel workbook.</div>';

    // Org mode shows each principal's own recent decisions, but has no export
    // route and no install log level to set -- both controls are local-only actions.
    if (!notApplicable('export_audit_log')) {
      html += '<div class="pf-row-controls pf-export-row"><div class="button primary" role="button" tabindex="0" aria-label="Export Audit Log" ' +
        dataAttr('export_audit_log', {}) + '>Export Audit Log…</div>';
      html += '<div class="pf-hint">' + esc(audit.export_hint) + '</div></div>';
    }

    if (!notApplicable('set_log_level')) {
      html += '<div class="card pf-card pf-audit-card">';
      html += rowHtml('Log level', '', segGroupHtml(LOG_LEVELS.map(function (lvl) {
        return { label: lvl, active: audit.log_level === lvl, action: 'set_log_level', payload: { level: lvl } };
      }), 'Log level'));
      html += rowHtml('Log file', '<span class="pf-audit-logfile">' + esc(audit.log_file) + '</span>', '', 'pf-row-sub');
      html += '</div>';
    }

    html += '<h2 class="pf-group-title">Recent decisions</h2>';
    html += '<div class="card pf-list pf-audit-list">';
    if (audit.recent.length === 0) {
      html += '<div class="pf-audit-row"><div class="pf-empty">Nothing logged yet.</div></div>';
    }
    audit.recent.forEach(function (a) {
      var badgeCls = a.decision === 'denied' || a.decision === 'rejected' ? 'denied' : (a.decision === 'auto_accepted' ? 'auto_accepted' : 'other');
      html += '<div class="pf-audit-row">';
      html += auditAgentHtml(a.agent);
      html += '<div class="pf-audit-connector">' + esc(a.connector) + '</div>';
      html += '<div class="pf-audit-tool">' + esc(a.tool) + '</div>';
      html += '<span class="badge ' + AUDIT_BADGE[badgeCls] + ' pf-audit-badge ' + badgeCls + '">' + esc(a.decision) + '</span>';
      html += '<div class="pf-audit-time">' + esc(a.time) + '</div>';
      html += '</div>';
    });
    html += '</div></div>';
    return html;
  }

  // settings_controller.audit_rows()'s `agent` -- agent_label.AgentLabel.to_dict(), the
  // same tiered wording the approval card and list use. A row with no agent (a log line from
  // before attribution) reads as unknown: never blank, never "Claude". No brand mark here --
  // the tier marker carries the distinction on a page this dense.
  var TIER_MARKERS = { attested: 'Verified', claimed: 'Not verified', unknown: 'Unknown' };
  // The approval card's badges for the same tiers: solid for verified, dashed for a claim.
  var TIER_BADGE = { attested: 'badge-solid', claimed: 'badge-dashed', unknown: 'badge-dashed' };

  function auditAgentHtml(agent) {
    agent = agent || {};
    var tier = TIER_MARKERS.hasOwnProperty(agent.tier) ? agent.tier : 'unknown';
    var headline = agent.headline || 'Unrecognised AI system';
    var text = agent.claim ? headline + ' \u201c' + agent.claim + '\u201d' : headline;
    return '<div class="pf-audit-agent pf-audit-agent-' + tier + '" data-agent-tier="' + tier + '" title="' + esc(text) + '">' +
      '<span class="pf-audit-agent-name">' + esc(text) + '</span><span class="badge ' + TIER_BADGE[tier] + ' pf-audit-tier ' + tier + '">' +
      esc(TIER_MARKERS[tier]) + '</span></div>';
  }

  // -------------------------------------------------------------------- //
  // AI systems (org mode, admin only -- ADR 0035 decision 3)
  // -------------------------------------------------------------------- //

  function renderAgents(state) {
    var agents = state.agents || { clients: [], stale_pins: [], registry: [] };
    var html = '<div class="pf-page">';
    html += '<h1 class="pf-page-title">AI systems</h1>';
    html += '<div class="pf-page-subtitle">Every OAuth client registered with this server names itself — anything that can reach the server can register as "ChatGPT". Pin a registration you have checked to the AI system it really is: only a pinned client is shown as verified, and a pin never moves to another registration.</div>';

    html += '<h2 class="pf-group-title">Registered clients</h2>';
    html += '<div class="card pf-list pf-agents-list">';
    if (agents.clients.length === 0) {
      html += '<div class="pf-agents-row"><div class="pf-agents-meta">No OAuth clients are registered yet.</div></div>';
    }
    agents.clients.forEach(function (c) {
      html += '<div class="pf-agents-row" data-agent-client="' + esc(c.client_id) + '">';
      html += '<div class="pf-agents-head"><div class="pf-agents-name">' +
        (c.client_name ? 'Registered as \u201c' + esc(c.client_name) + '\u201d' : 'No name registered') + '</div>';
      html += '<div class="pf-agents-meta">last used ' + esc(c.last_used) + '</div></div>';
      html += '<div class="pf-agents-meta">' + esc(c.client_id) + '</div>';
      html += '<div class="pf-agents-controls">';
      if (c.pinned_agent_id) {
        html += '<span>Pinned to <strong>' + esc(c.pinned_agent_name) + '</strong></span>';
        html += '<div class="button secondary" role="button" tabindex="0" aria-label="Unpin" ' +
          dataAttr('unpin_agent_client', { client_id: c.client_id }) + '>Unpin</div>';
      } else {
        html += '<span>Not verified. Pin to:</span>';
        agents.registry.forEach(function (r) {
          html += '<div class="button secondary" role="button" tabindex="0" aria-label="Pin to ' + esc(r.name) + '" ' +
            dataAttr('pin_agent_client', { client_id: c.client_id, agent_id: r.id }) + '>' + esc(r.name) + '</div>';
        });
      }
      html += '</div></div>';
    });
    html += '</div>';

    if (agents.stale_pins.length > 0) {
      html += '<h2 class="pf-group-title">Stale pins</h2>';
      html += '<div class="pf-page-subtitle">These registrations were removed after going unused. Their pins no longer apply to anything; a client that registers again is a new registration and starts unverified.</div>';
      html += '<div class="card pf-list pf-agents-list">';
      agents.stale_pins.forEach(function (p) {
        html += '<div class="pf-agents-row" data-agent-stale-pin="' + esc(p.client_id) + '">';
        html += '<div class="pf-agents-head"><div class="pf-agents-name">' + esc(p.agent_name) + '</div></div>';
        html += '<div class="pf-agents-meta">' + esc(p.client_id) + '</div>';
        html += '<div class="pf-agents-controls"><div class="button secondary" role="button" tabindex="0" aria-label="Remove pin" ' +
          dataAttr('unpin_agent_client', { client_id: p.client_id }) + '>Remove pin</div></div>';
        html += '</div>';
      });
      html += '</div>';
    }
    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // About
  // -------------------------------------------------------------------- //

  function renderAbout(state) {
    var about = state.about;
    var html = '<div class="pf-about-page">';
    html += '<div class="pf-about-icon">PF</div>';
    html += '<h1 class="pf-about-name">PrivacyFence</h1>';
    html += '<div class="pf-about-version">Version ' + esc(about.version) + '</div>';
    html += '<div class="pf-about-desc">Human control and policy enforcement for AI access to enterprise data.</div>';
    html += '<div class="pf-link pf-about-repo" role="button" tabindex="0" aria-label="Open GitHub repository" ' +
      dataAttr('open_repo', {}) + '>' + esc(about.repo_url.replace('https://', '')) + ' ↗</div>';
    html += '<div class="pf-about-license">' + esc(about.license) + '</div>';
    html += '<div class="pf-about-buttons">';
    // check_for_updates/quit_app: a version-update check against GitHub
    // Releases and shutting down the daemon are both local-install
    // concepts -- neither has an org route (org's own routes_settings.py
    // never mounts either), so both are unconditionally in this
    // capability set's not_applicable_actions for org mode.
    if (!notApplicable('check_for_updates')) {
      html += '<div class="button secondary" role="button" tabindex="0" aria-label="Check for Updates" ' +
        dataAttr('check_for_updates', {}) + '>Check for Updates</div>';
    }
    if (!notApplicable('quit_app')) {
      html += '<div class="button danger" role="button" tabindex="0" aria-label="Quit PrivacyFence" ' +
        dataAttr('quit_app', {}) + '>Quit PrivacyFence</div>';
    }
    html += '</div></div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Top-level render
  // -------------------------------------------------------------------- //

  function renderSection(state) {
    // A section CAPS itself hides never renders, even if `ui.section`
    // somehow still names it (e.g. a stale `data-nav` click recorded before
    // a capabilities-driven re-render, or a route naming it directly via
    // window.__pfInitialSection) -- falls back to whichever of Auto-accept/
    // General this mode actually draws, the same choice `ui.section`'s own
    // initial value above makes.
    var section = ui.section;
    if (CAPS.sections[section] === false) {
      section = CAPS.sections.general ? 'general' : 'auto_accept';
    }
    switch (section) {
      case 'connectors': return renderConnectors(state);
      case 'auto_accept': return renderAutoAccept(state);
      case 'privacy': return renderPrivacy(state);
      case 'audit': return renderAudit(state);
      case 'agents': return renderAgents(state);
      case 'about': return renderAbout(state);
      default: return renderGeneral(state);
    }
  }

  // -------------------------------------------------------------------- //
  // Telegram sign-in modal (see settings_controller.py's telegram_start_
  // auth/telegram_submit_code/telegram_submit_2fa/telegram_cancel_auth)
  // -------------------------------------------------------------------- //

  var TELEGRAM_STEP_COPY = {
    phone: {
      title: 'Sign in to Telegram', desc: 'Phone number, with country code (e.g. +1234567890):',
      placeholder: '+1234567890', type: 'text', submitLabel: 'Send Code', submitAction: 'telegram_start_auth', field: 'phone',
    },
    code: {
      title: 'Enter verification code', desc: 'Telegram sent a code to the number above.',
      placeholder: 'Code', type: 'text', submitLabel: 'Authorize', submitAction: 'telegram_submit_code', field: 'code',
    },
    password: {
      title: 'Two-step verification', desc: 'Enter your Telegram two-step verification password.',
      placeholder: 'Password', type: 'password', submitLabel: 'Submit', submitAction: 'telegram_submit_2fa', field: 'password',
    },
  };

  function renderTelegramModal(state) {
    if (!ui.telegramModalOpen) return '';
    var auth = state.telegram_auth || { step: null, error: '' };
    var step = auth.step || 'phone';
    var copy = TELEGRAM_STEP_COPY[step];
    var busy = (state.connectors.find(function (c) { return c.key === 'telegram'; }) || {}).busy;

    var html = '<div class="pf-modal-overlay" role="presentation">';
    html += '<div class="pf-modal" role="dialog" aria-modal="true" aria-label="' + esc(copy.title) + '">';
    html += '<div class="pf-modal-title">' + esc(copy.title) + '</div>';
    html += '<div class="pf-modal-desc">' + esc(copy.desc) + '</div>';
    if (auth.error) html += '<div class="pf-modal-error">' + esc(auth.error) + '</div>';
    html += '<input type="' + copy.type + '" class="field pf-modal-input" placeholder="' + esc(copy.placeholder) +
      '" data-telegram-field="' + copy.field + '" aria-label="' + esc(copy.placeholder) + '"' + (busy ? ' disabled' : '') + '/>';
    html += '<div class="pf-modal-buttons">';
    html += '<div class="button secondary" role="button" tabindex="0" aria-label="Cancel Telegram sign-in" data-telegram-cancel="1">Cancel</div>';
    html += '<div class="button primary" role="button" tabindex="0" aria-label="' + esc(copy.submitLabel) +
      '" data-telegram-submit="' + copy.submitAction + '"' + (busy ? ' aria-disabled="true"' : '') + '>' +
      (busy ? 'Working…' : esc(copy.submitLabel)) + '</div>';
    html += '</div></div></div>';
    return html;
  }

  // A narrow tabstrip scrolls sideways inside itself, and a re-render starts
  // it back at the left: bring the selected tab into view without moving the
  // page itself (scrollIntoView would also scroll the document vertically).
  function revealSelectedTabs() {
    document.querySelectorAll('.pf-nav, .pf-subnav').forEach(function (strip) {
      var tab = strip.querySelector('[aria-selected="true"]');
      if (!tab || strip.scrollWidth <= strip.clientWidth) return;
      strip.scrollLeft = Math.max(0, tab.offsetLeft - strip.offsetLeft - (strip.clientWidth - tab.offsetWidth) / 2);
    });
  }

  function render(state) {
    pyState = state;
    // Auto-close the modal once Python reports the sign-in is no longer in
    // progress (success or explicit cancel) -- see telegram_cancel_auth/
    // the success branches of telegram_submit_code/telegram_submit_2fa.
    // Gated on telegramAuthWasActive (see its own comment above): a modal
    // that's open but has never actually submitted anything also has
    // telegram_auth.step === null, and must not be closed out from under
    // the user before they've even typed a phone number.
    var authStep = state.telegram_auth && state.telegram_auth.step;
    if (authStep) ui.telegramAuthWasActive = true;
    if (ui.telegramModalOpen && ui.telegramAuthWasActive && !authStep && !(state.telegram_auth && state.telegram_auth.error)) {
      ui.telegramModalOpen = false;
      ui.telegramAuthWasActive = false;
    }
    // .split (base.css): the nav and the content, one column when narrow
    // and two from 900 px up (see _CSS).
    var html = '<div class="split pf-settings-layout">' + renderNav(state) + '<div class="pf-content">';
    if (state.error) {
      html += '<div class="card card-danger pf-banner pf-error-banner" role="alert"><div>' + esc(state.error) + '</div>' +
        '<div class="pf-banner-dismiss pf-error-dismiss" role="button" tabindex="0" aria-label="Dismiss error" data-dismiss-error="1">✕</div></div>';
    }
    html += renderSection(state) + '</div></div>';
    html += renderTelegramModal(state);
    document.getElementById('app').innerHTML = html;
    revealSelectedTabs();
    if (ui.telegramModalOpen) {
      var input = document.querySelector('[data-telegram-field]');
      if (input) input.focus();
    }
  }

  window.__pfRender = render;
  window.__pfDebugHook = { ui: ui, render: render, TELEGRAM_STEP_COPY: TELEGRAM_STEP_COPY, onClick: null, onKeydown: null };

  // -------------------------------------------------------------------- //
  // Event delegation
  // -------------------------------------------------------------------- //

  function onClick(e) {
    var navEl = e.target.closest('[data-nav]');
    if (navEl) { ui.section = navEl.getAttribute('data-nav'); render(pyState); return; }

    var aaConnFilterEl = e.target.closest('[data-aa-connector-filter]');
    if (aaConnFilterEl) {
      var cname = aaConnFilterEl.getAttribute('data-aa-connector-filter');
      var idx = ui.aaConnectorFilter.indexOf(cname);
      if (idx === -1) { ui.aaConnectorFilter.push(cname); } else { ui.aaConnectorFilter.splice(idx, 1); }
      render(pyState);
      return;
    }

    var aaFamFilterEl = e.target.closest('[data-aa-family-filter]');
    if (aaFamFilterEl) {
      var fam = aaFamFilterEl.getAttribute('data-aa-family-filter');
      var famIdx = ui.aaFamilyFilter.indexOf(fam);
      if (famIdx === -1) { ui.aaFamilyFilter.push(fam); } else { ui.aaFamilyFilter.splice(famIdx, 1); }
      render(pyState);
      return;
    }

    var aaExpandEl = e.target.closest('[data-aa-expand]');
    if (aaExpandEl) {
      var ruleId = aaExpandEl.getAttribute('data-aa-expand');
      ui.aaExpanded[ruleId] = !ui.aaExpanded[ruleId];
      render(pyState);
      return;
    }

    var aaVerbEl = e.target.closest('[data-aa-verb-toggle]');
    if (aaVerbEl) {
      var verb = aaVerbEl.getAttribute('data-aa-verb-toggle');
      ui.aaCheckedVerbs[verb] = !ui.aaCheckedVerbs[verb];
      render(pyState);
      return;
    }

    var aaAddEl = e.target.closest('[data-aa-add]');
    if (aaAddEl) { submitAddPolicyRule(); return; }

    var privacyNavEl = e.target.closest('[data-privacy-nav]');
    if (privacyNavEl) { ui.privacyGroup = privacyNavEl.getAttribute('data-privacy-nav'); render(pyState); return; }

    var dismissEl = e.target.closest('[data-dismiss-error]');
    if (dismissEl) { pyState.error = ''; render(pyState); return; }

    var welcomeDismissEl = e.target.closest('[data-dismiss-welcome]');
    if (welcomeDismissEl) { ui.welcomeBannerDismissed = true; render(pyState); return; }

    var telegramAuthEl = e.target.closest('[data-telegram-auth]');
    if (telegramAuthEl) { ui.telegramModalOpen = true; ui.telegramAuthWasActive = false; render(pyState); return; }

    var telegramCancelEl = e.target.closest('[data-telegram-cancel]');
    if (telegramCancelEl) {
      ui.telegramModalOpen = false;
      ui.telegramAuthWasActive = false;
      post('telegram_cancel_auth', {});
      render(pyState);
      return;
    }

    var telegramSubmitEl = e.target.closest('[data-telegram-submit]');
    if (telegramSubmitEl) { submitTelegramModal(telegramSubmitEl.getAttribute('data-telegram-submit')); return; }

    var repoEl = e.target.closest('[data-action="open_repo"]');
    if (repoEl) { post('open_repo', {}); return; }

    // Client-only, like open_repo above -- Notification.requestPermission()
    // is a browser API, never a message to Python (there is no server-side
    // state to mutate; see renderNotificationsCard's own comment). Re-render
    // once the browser's own dialog resolves so the card reflects the
    // outcome (granted/denied) immediately rather than only on next visit.
    var notifEl = e.target.closest('[data-notif-enable]');
    if (notifEl) {
      if (typeof Notification !== 'undefined' && Notification.requestPermission) {
        Notification.requestPermission().then(function () { render(pyState); });
      }
      return;
    }

    var actionEl = e.target.closest('[data-action]');
    if (actionEl) {
      // A toggle's checkbox: keep it showing what Python says (the next
      // render), not the browser's own flip -- see toggleHtml.
      if (actionEl.tagName === 'INPUT' && actionEl.type === 'checkbox') e.preventDefault();
      var action = actionEl.getAttribute('data-action');
      var payload = {};
      try { payload = JSON.parse(actionEl.getAttribute('data-payload') || '{}'); } catch (err) { payload = {}; }
      post(action, payload);
    }
  }

  function submitTelegramModal(action) {
    var input = document.querySelector('[data-telegram-field]');
    var value = input ? input.value : '';
    var field = input ? input.getAttribute('data-telegram-field') : 'phone';
    var payload = {};
    payload[field] = value;
    post(action, payload);
  }


  // The Auto-accept page's own "Add rule" submit -- reads the current form state straight off
  // the DOM (the value field, whichever verb chips are checked) rather than from `ui`, since only
  // the selected group id is actually tracked there (see renderAutoAccept). Client-side no-ops
  // (rather than posting nothing useful) when no verb is checked -- add_policy_rule itself would
  // also just no-op, but skipping the round trip is cheap and avoids a pointless snapshot re-push.
  function submitAddPolicyRule() {
    var checked = Object.keys(ui.aaCheckedVerbs || {}).filter(function (v) { return ui.aaCheckedVerbs[v]; });
    if (!ui.aaGroup || checked.length === 0) return;
    var valueInput = document.querySelector('[data-aa-value]');
    post('add_policy_rule', { group: ui.aaGroup, value: valueInput ? valueInput.value : '', verbs: checked });
    ui.aaCheckedVerbs = {};
    ui.aaValue = '';
  }

  // No blur-commit fields are left (the Auto-accept page's own value field commits live via
  // onInput instead -- see its own comment) -- kept wired (a no-op) rather than unregistered, so a
  // future blur-commit field doesn't also need to re-add the listener itself.
  function onBlur(e) {}

  function onChange(e) {
    var el = e.target;
    // A scope change resets the value/verb selections below it -- a different scope's value has a
    // different shape (a Drive folder id isn't a Jira project key) and its own, generally different,
    // set of governable verbs, so carrying either over would at best be meaningless and at worst
    // silently submit the wrong thing.
    if (el.tagName === 'SELECT' && el.hasAttribute('data-aa-group-select')) {
      ui.aaGroup = el.value;
      ui.aaValue = '';
      ui.aaCheckedVerbs = {};
      render(pyState);
    }
  }

  function onContextMenu(e) {
    var copyEl = e.target.closest('[data-copy-id]');
    if (!copyEl) return;
    var id = copyEl.getAttribute('data-copy-id');
    if (!id) return;
    e.preventDefault();
    copyToClipboard(id);
    showCopyToast(e.clientX, e.clientY, 'Copied ID');
  }

  function onInput(e) {
    var el = e.target;
    if (el.hasAttribute('data-aa-search')) {
      ui.aaSearch = el.value;
      var pos = el.selectionStart;
      render(pyState);
      var fresh = document.querySelector('[data-aa-search]');
      if (fresh) { fresh.focus(); try { fresh.setSelectionRange(pos, pos); } catch (err) {} }
      return;
    }
    // The value field commits live (not on blur/Enter, unlike a v1-era text field -- see this
    // module's own docstring) so it survives a verb-chip toggle's own re-render without losing
    // what was typed; there's no server round trip until "Add rule" is actually clicked.
    if (el.hasAttribute('data-aa-value')) {
      ui.aaValue = el.value;
      var vpos = el.selectionStart;
      render(pyState);
      var freshValue = document.querySelector('[data-aa-value]');
      if (freshValue) { freshValue.focus(); try { freshValue.setSelectionRange(vpos, vpos); } catch (err) {} }
    }
  }

  function onKeydown(e) {
    if (e.key === 'Escape' && ui.telegramModalOpen) {
      ui.telegramModalOpen = false;
      ui.telegramAuthWasActive = false;
      post('telegram_cancel_auth', {});
      render(pyState);
      return;
    }
    // Enter/Space activates any of our ARIA-role'd interactive elements
    // (role="button"/"tab"/"radio"/"switch"/"checkbox") the same way a
    // click does -- they're plain <div>s with tabindex="0", not native
    // <button>s, so the browser doesn't do this for free.
    if ((e.key === 'Enter' || e.key === ' ') && e.target.tagName !== 'INPUT' && e.target.closest) {
      var interactive = e.target.closest('[role="button"], [role="tab"], [role="radio"], [role="switch"], [role="checkbox"]');
      if (interactive) {
        e.preventDefault();
        interactive.click();
        return;
      }
    }
    if (e.key !== 'Enter') return;
    var el = e.target;
    if (!el.tagName || el.tagName !== 'INPUT') return;
    // A toggle's checkbox answers Space natively; Enter as well, as the
    // div-based switch it replaced did.
    if (el.getAttribute('role') === 'switch') { e.preventDefault(); el.click(); return; }
    if (el.hasAttribute('data-aa-value')) {
      submitAddPolicyRule();
      return;
    }
    if (el.hasAttribute('data-telegram-field')) {
      var state = pyState || {};
      var step = ((state.telegram_auth || {}).step) || 'phone';
      submitTelegramModal(TELEGRAM_STEP_COPY[step].submitAction);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.body.addEventListener('click', onClick);
    document.body.addEventListener('blur', onBlur, true);
    document.body.addEventListener('change', onChange);
    document.body.addEventListener('input', onInput);
    document.body.addEventListener('keydown', onKeydown);
    document.body.addEventListener('contextmenu', onContextMenu);
    render(window.__pfInitialState);
  });
})();
"""


_ALL_SECTIONS = ("general", "connectors", "auto_accept", "privacy", "audit", "agents", "about")

# The four actions web/routes_settings.py's own bridge shim intercepts
# client-side rather than forwarding to the generic dispatcher (that
# module's own docstring) -- none has an ACTION_SCOPES entry at all (it
# isn't one of that dispatcher's actions), so org_settings_scope.
# NOT_APPLICABLE_ACTIONS doesn't cover any of them either. Three are
# local-desktop-install concepts with no org route or org-mode meaning
# (checking this *install's* GitHub Releases feed, installing *this org's*
# own config bundle onto itself, shutting down the whole shared daemon);
# open_repo (a plain link) is deliberately not included -- equally
# applicable in every mode. check_for_updates is the About page's own
# action name for what SettingsController implements as
# check_for_updates_now -- a naming mismatch that predates this phase (see
# this phase's own PR description's "Follow-ups noticed"), listed here by
# the name the JS below actually posts.
_LOCAL_ONLY_BESPOKE_ACTIONS: frozenset[str] = frozenset({
    "install_org_config", "export_audit_log", "quit_app", "check_for_updates",
})


def _capabilities_for(mode: str, *, is_admin: bool) -> dict[str, Any]:
    """Which of the inner-nav sections this ``(mode, is_admin)``
    combination gets, and which individual controls (see ``dataAttr``/
    ``toggleHtml`` call sites throughout ``_JS``) must never draw at all --
    kept out of ``state`` itself (a separate ``window.__pfCapabilities``
    global, see ``build_html``) so ``state`` stays exactly what the caller
    passed in, byte for byte, the same invariant
    test_settings_window_html.py's own ``test_state_round_trips_byte_for_
    byte`` already checks.

    Local mode (every existing caller) gets every section and no
    suppressed action -- this function must be a no-op for ``mode !=
    ORG_MODE``, since that's what keeps every local rendering path
    (webview and web alike) independent of org mode's filtering.

    Org mode gets ``org_settings_scope.NOT_APPLICABLE_ACTIONS`` (every
    action with no real org route at all -- see that module for the
    single source of truth) plus section-level decisions this function
    itself owns, all narrower than "has an org route": Connectors has no
    applicable action *and* nothing else worth showing (no read-only
    connector list exists for org mode today), so the whole section is
    hidden rather than rendered empty; Audit Log is shown to every
    principal as a read-only list of their own recent decisions, its
    local-only export and log-level controls suppressed through
    ``not_applicable_actions``; AI systems (ADR 0035 decision 3) is org-admin-only and
    never shown in local mode; General and Privacy
    Filter are further gated on ``is_admin`` -- the admin-only privacy/PII
    split (every action either page
    can post is itself ``admin_only`` in ``ACTION_SCOPES``, so a non-admin
    who somehow reached one would have every mutation 403 anyway; hiding
    the page is the same authorization decision, applied to rendering).
    """
    if mode != ORG_MODE:
        return {
            "mode": LOCAL_MODE, "is_admin": False,
            # The AI-system pin page is org-only -- local mode has no DCR
            # registrations to pin (settings.yaml's agent_overrides: only relabels, ADR 0037).
            "sections": {**dict.fromkeys(_ALL_SECTIONS, True), "agents": False},
            "not_applicable_actions": [],
        }
    return {
        "mode": ORG_MODE, "is_admin": is_admin,
        "sections": {
            "general": is_admin, "connectors": False, "auto_accept": True,
            "privacy": is_admin, "audit": True, "agents": is_admin, "about": True,
        },
        "not_applicable_actions": sorted(NOT_APPLICABLE_ACTIONS | _LOCAL_ONLY_BESPOKE_ACTIONS),
    }


def build_html(
    state: dict, *, nonce: str | None = None, initial_section: str | None = None,
    mode: str = LOCAL_MODE, is_admin: bool = False,
) -> str:
    """Full self-contained HTML document for the settings window's WKWebView
    (``mode="local"``) *and* for
    org mode's own ``GET /settings``/``GET /settings/privacy`` -- one
    implementation rendering a capability-filtered subset for each, not one
    page (see ADR 0033 and ADR 0032): the nav items and page content below
    differ by mode/``is_admin``, but every section both modes keep
    (Auto-accept; General/Privacy Filter
    for an org admin) is the exact same template, reading the exact same
    ``state`` shape, posting through the exact same bridge.

    ``state`` is embedded directly as ``window.__pfInitialState`` so the
    first paint needs no round trip to Python -- see this module's
    docstring for the bridge protocol Python's re-renders (``window.
    __pfRender``) follow afterwards. Unchanged by ``mode``/``is_admin``:
    those two only ever affect the separate ``window.__pfCapabilities``
    global below, never ``state`` itself.

    ``nonce``: the
    current response's CSP nonce (``request.state.csp_nonce``) -- this
    fragment is rendered fresh on every ``GET /settings`` and dropped into
    web/routes_settings.py's own web_shell.wrap() call, which must be given
    that exact same nonce -- one document, one Content-Security-Policy
    header. Defaults to a fresh one when omitted (every real caller passes
    the actual per-request value explicitly).

    ``initial_section``: a deliberate, narrow exception
    to ``ui.section`` otherwise being purely client-side state (see this
    module's own docstring) -- ``GET /settings/connectors`` passes
    ``"connectors"`` so a link opened while un-onboarded lands
    directly on the screen that unblocks the user, instead of ``/settings``'s
    default General page. ``None`` (every other route) emits no script at
    all, leaving the JS's own ``'general'`` fallback exactly as before.
    Org mode's callers pass ``"auto_accept"``/``"privacy"`` for
    the same reason -- org mode's own General page is empty (hidden
    entirely, in fact) for a non-admin principal, so falling back to it
    would land every non-admin on a blank nav selection.

    ``mode``/``is_admin``: see ``_capabilities_for`` above for
    exactly what each combination hides. Both default to local mode's own
    values, so a call site that passes neither (every one except
    web/routes_settings.py's org routes, which pass ``mode="org"``)
    renders the full local page.
    """
    nonce = nonce or secrets.token_urlsafe(18)
    state_json = json.dumps(state)
    caps_json = json.dumps(_capabilities_for(mode, is_admin=is_admin))
    section_script = ""
    if initial_section is not None:
        section_script = f'<script nonce="{nonce}">window.__pfInitialSection = {json.dumps(initial_section)};</script>'
    return (
        "<title>PrivacyFence Settings</title>"
        f'<style nonce="{nonce}">{DOCUMENT_CSS}{_CSS}</style>'
        '<div id="app" class="shell pf-settings"></div>'
        f'<script nonce="{nonce}">window.__pfInitialState = {state_json};</script>'
        f'<script nonce="{nonce}">window.__pfCapabilities = {caps_json};</script>'
        f"{section_script}"
        f'<script nonce="{nonce}">{_JS}</script>'
    )
