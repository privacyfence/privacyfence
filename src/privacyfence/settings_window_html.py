"""HTML/CSS/vanilla-JS for the webview settings window (settings_window.py).

Visually transcribed from the design source (a Claude Design prototype
export, not shipped with this repo -- see the PR description for where it
lives) -- colors, spacing, radii, and the toggle/segmented-control visuals
below are copied from that file's inline styles, not approximated. What
*isn't* transcribed is that file's own rendering machinery (a small
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
Text inputs (grant name/id -- through P5; the Auto-accept page's own value
field, since P6) commit on blur/Enter, not per keystroke, so a bridge
round-trip mid-typing can't steal focus/cursor position; toggles/segmented
controls/buttons act immediately on click since they're discrete, not free
text. The Auto-accept page's own search/filter inputs are the one exception
(P6, following the same pattern this module's pre-P6 rules search already
used): every keystroke re-renders, since filtering that list is itself the
whole point of typing into it, and ``onInput`` below restores focus/cursor
position across that re-render the same way it already did for the old
search box.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from .web.org_settings_scope import LOCAL_MODE, ORG_MODE, NOT_APPLICABLE_ACTIONS

# The settings page's own
# palette is restyled onto the same tokens the approval card already
# defines (resources/tokens.css, extracted from resources/approval_window/
# styles.css's own :root block -- see that file's own docstring for why it
# stays a separate export rather than a single shared @import source),
# rather than the two documents' hand-tuned, independently-drifting hex
# values they had before this phase. This is a palette swap, not a layout
# change (§16.8's risk #5) -- every rule below keeps its original spacing/
# radius/structure; only color values became var(--pf-*)/var(--color-*)
# references, which is also what makes @media(prefers-color-scheme: dark)
# (embedded in tokens.css) apply here for the first time, with no second
# dark block needed in this file.
_TOKENS_CSS = (Path(__file__).parent / "resources" / "tokens.css").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------- #
# CSS -- values copied from the design source's inline styles, then (see
# above) recolored onto the shared token set via a small number of
# settings-page-specific role aliases (--pf-*) defined here rather than
# referencing --color-* directly everywhere below: a page-local name for
# "the muted secondary text color" reads at the call site, and it's one
# place to reconsider which --color-neutral-N step plays that role, instead
# of several hundred. Every --pf-* value here is itself just a --color-*
# reference (or, for the two /-tint colors that need one, a color-mix of
# one), so nothing below needs its own dark-mode override: tokens.css's
# @media block already redefines the --color-* values these derive from.
# ---------------------------------------------------------------------------- #

_CSS = """
:root {
  --pf-page-bg: var(--color-bg);
  --pf-content-bg: var(--color-bg);
  --pf-nav-bg: var(--color-surface);
  --pf-surface: var(--color-surface);
  --pf-surface-2: var(--color-neutral-200);
  --pf-border: var(--color-divider);
  --pf-border-strong: var(--color-neutral-400);
  --pf-text: var(--color-text);
  --pf-text-muted: var(--color-neutral-600);
  --pf-text-dim: var(--color-neutral-500);
  --pf-accent: var(--color-accent);
  --pf-danger: var(--color-danger);
  --pf-danger-tint: var(--color-danger-tint);
  --pf-danger-border: color-mix(in srgb, var(--color-danger) 35%, transparent);
  --pf-warn: #8a5a00;
  --pf-warn-tint: #fff3cd;
}
@media (prefers-color-scheme: dark) {
  :root {
    --pf-warn: #e0a94a;
    --pf-warn-tint: color-mix(in srgb, #e0a94a 20%, transparent);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; background: var(--pf-page-bg); }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', Helvetica, Arial, sans-serif;
  color: var(--pf-text);
  overflow: hidden;
}
::selection { background: rgba(0, 113, 227, .25); }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--pf-border-strong); border-radius: 6px; }
::-webkit-scrollbar-track { background: transparent; }
input[type=text]:focus { outline: 2px solid var(--pf-accent); outline-offset: 0; }

#app { display: flex; height: 100vh; overflow: hidden; }

/* ---- Left nav ---- */
.pf-nav {
  width: 190px; flex-shrink: 0; background: var(--pf-nav-bg); border-right: 1px solid var(--pf-border);
  padding: 14px 10px; display: flex; flex-direction: column; gap: 2px; height: 100%;
}
.pf-navitem {
  padding: 8px 12px; border-radius: 7px; font-size: 13px; cursor: pointer; font-weight: 400;
  color: var(--pf-text); background: transparent;
}
.pf-navitem.active { font-weight: 600; background: var(--pf-accent); color: #fff; }
.pf-nav-spacer { flex: 1; }
.pf-nav-version { padding: 8px 10px; font-size: 11px; color: var(--pf-text-dim); }

/* ---- Content shell ---- */
.pf-content { flex: 1; overflow: hidden; display: flex; background: var(--pf-content-bg); min-width: 0; }
.pf-page { flex: 1; overflow-y: auto; padding: 36px 44px; }
.pf-page-title { font-size: 22px; font-weight: 700; color: var(--pf-text); margin: 0 0 22px; }
.pf-page-subtitle { font-size: 12px; color: var(--pf-text-muted); margin-bottom: 22px; max-width: 600px; line-height: 1.5; }

/* ---- Error banner ---- */
.pf-error-banner {
  background: var(--pf-danger-tint); border: 1px solid var(--pf-danger-border); color: var(--pf-danger); border-radius: 8px;
  padding: 10px 14px; font-size: 12.5px; margin: 16px 44px 0; display: flex;
  align-items: center; justify-content: space-between; gap: 12px;
}
.pf-error-dismiss { cursor: pointer; color: var(--pf-danger); font-weight: 600; flex-shrink: 0; }
.pf-update-banner { border-color: var(--pf-accent); }
.pf-welcome-banner { border-color: var(--pf-accent); }
.pf-welcome-dismiss { cursor: pointer; color: var(--pf-text-muted); font-weight: 600; flex-shrink: 0; }

/* ---- Cards / rows shared across pages ---- */
.pf-card {
  background: var(--pf-surface); border: 1px solid var(--pf-border); border-radius: 10px; padding: 16px 20px;
  margin-bottom: 16px; max-width: 620px;
}
.pf-card-row { display: flex; align-items: center; justify-content: space-between; }
.pf-card-title { font-size: 14px; font-weight: 600; color: var(--pf-text); }
.pf-card-desc { font-size: 12px; color: var(--pf-text-muted); margin-top: 3px; max-width: 440px; line-height: 1.4; }
.pf-divider { height: 1px; background: var(--pf-border); margin: 14px 0; }
.pf-subrow { display: flex; align-items: center; justify-content: space-between; padding: 4px 0; }
.pf-subrow-label { font-size: 13px; color: var(--pf-text); }

/* ---- Toggle switch ---- */
.pf-toggle {
  width: 40px; height: 24px; border-radius: 12px; cursor: pointer; transition: background .15s;
  background: var(--pf-border-strong); position: relative; flex-shrink: 0;
}
.pf-toggle.on { background: var(--pf-accent); }
.pf-toggle.disabled { cursor: default; opacity: .5; }
.pf-knob {
  width: 20px; height: 20px; border-radius: 50%; background: #fff; position: relative;
  top: 2px; left: 2px; transition: left .15s; box-shadow: 0 1px 2px rgba(0,0,0,.25);
}
.pf-toggle.on .pf-knob { left: 20px; }

/* ---- Buttons ---- */
.pf-btn-primary {
  background: var(--pf-accent); color: #fff; border: none; border-radius: 7px; padding: 7px 14px;
  font-size: 13px; font-weight: 500; cursor: pointer;
}
.pf-btn-secondary {
  background: var(--pf-surface-2); color: var(--pf-text); border: none; border-radius: 7px; padding: 8px 16px;
  font-size: 13px; font-weight: 500; cursor: pointer;
}
.pf-btn-danger {
  background: var(--pf-content-bg); color: var(--pf-danger); border: 1px solid var(--pf-danger-border); border-radius: 7px;
  padding: 8px 16px; font-size: 13px; font-weight: 500; cursor: pointer;
}
.pf-link { font-size: 12.5px; color: var(--pf-accent); cursor: pointer; }
.pf-link-danger { font-size: 12px; color: var(--pf-danger); cursor: pointer; white-space: nowrap; }

/* ---- Segmented controls ---- */
.pf-seg-group { display: flex; background: var(--pf-surface-2); border-radius: 7px; padding: 2px; flex-shrink: 0; }
.pf-seg-btn { padding: 5px 12px; font-size: 12px; font-weight: 500; border-radius: 5px; cursor: pointer; color: var(--pf-text-muted); }
.pf-seg-btn.plain-active { background: var(--pf-content-bg); box-shadow: 0 1px 2px rgba(0,0,0,.15); color: var(--pf-text); }
.pf-seg-btn.policy-allow { background: #0071e3; color: #fff; }
.pf-seg-btn.policy-redact { background: #b76e00; color: #fff; }
.pf-seg-btn.policy-block { background: #d92d20; color: #fff; }

/* ---- Text inputs ---- */
.pf-input {
  border: 1px solid var(--pf-border); border-radius: 6px; padding: 5px 8px; font-size: 12.5px; background: var(--pf-content-bg);
}
.pf-input-mono { font-family: ui-monospace, monospace; }
select.pf-input { cursor: pointer; }

/* ---- Connectors page ---- */
.pf-connector-row {
  display: flex; align-items: center; gap: 14px; padding: 12px 4px; border-bottom: 1px solid var(--pf-border);
  max-width: 760px;
}
.pf-connector-icon {
  width: 34px; height: 34px; border-radius: 9px; background: var(--pf-surface-2); display: flex;
  align-items: center; justify-content: center; flex-shrink: 0; overflow: hidden;
}
.pf-connector-icon img { width: 22px; height: 22px; object-fit: contain; }
.pf-connector-label { width: 150px; font-size: 13.5px; color: var(--pf-text); font-weight: 500; flex-shrink: 0; }
.pf-pill { font-size: 11px; padding: 3px 9px; border-radius: 10px; white-space: nowrap; }
.pf-pill-connected { background: rgba(0,113,227,.1); color: var(--pf-accent); }
.pf-pill-neutral { background: var(--pf-surface-2); color: var(--pf-text-muted); }
.pf-pill-warn { background: var(--pf-warn-tint); color: var(--pf-warn); }
.pf-pill-missing { background: var(--pf-danger-tint); color: var(--pf-danger); }
.pf-spacer { flex: 1; }
.pf-auth-link { font-size: 12.5px; color: var(--pf-accent); cursor: pointer; white-space: nowrap; }
.pf-auth-link.disabled { color: var(--pf-text-dim); cursor: default; pointer-events: none; }

/* ---- Privacy's 2-pane layout -- Auto-accept (below) is a single flat page, no subnav, since P6
   replaced its old per-connector subnav with one filterable list ---- */
.pf-subnav {
  width: 170px; flex-shrink: 0; background: var(--pf-surface); border-right: 1px solid var(--pf-border);
  padding: 12px 10px; display: flex; flex-direction: column; overflow-y: auto;
}
.pf-subnav-search { margin-bottom: 10px; width: 100%; }
.pf-subnav-item {
  padding: 7px 10px; border-radius: 7px; font-size: 13px; cursor: pointer; display: flex;
  justify-content: space-between; margin-bottom: 1px; color: var(--pf-text); font-weight: 400;
}
.pf-subnav-item.active { background: var(--pf-accent); color: #fff; font-weight: 600; }
.pf-subnav-count { font-size: 11px; opacity: .75; }
.pf-detail-page { flex: 1; overflow-y: auto; padding: 28px 36px; }
.pf-detail-title { font-size: 18px; font-weight: 700; color: var(--pf-text); margin-bottom: 2px; }
.pf-detail-subtitle { font-size: 12px; color: var(--pf-text-muted); margin-bottom: 20px; max-width: 520px; line-height: 1.5; }

/* ---- Recent-decisions group heading (Audit page) / verb-selection chips (Auto-accept's own
   "add a rule" form, below) ---- */
.pf-group-title { font-size: 13px; font-weight: 600; color: var(--pf-text-muted); margin-bottom: 8px; }
.pf-caps-row { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
.pf-cap-chip { padding: 4px 10px; border-radius: 5px; font-size: 11px; cursor: pointer; font-weight: 500; background: var(--pf-surface-2); color: var(--pf-text-muted); }
.pf-cap-chip.on { background: var(--pf-accent); color: #fff; }

/* ---- Auto-accept (policy v2) -- P6 ---- */
.pf-rules-empty { font-size: 13px; color: var(--pf-text-dim); }
.pf-aa-filterbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 18px; max-width: 720px; }
.pf-aa-search { flex: 1 1 220px; min-width: 180px; }
.pf-fchip {
  font-size: 11.5px; font-weight: 500; padding: 4px 10px; border-radius: 12px; cursor: pointer;
  background: var(--pf-surface-2); color: var(--pf-text-muted); white-space: nowrap;
}
.pf-fchip.active { background: var(--pf-accent); color: #fff; }
.pf-verb-chip {
  display: inline-block; font-size: 10.5px; font-weight: 600; padding: 2px 7px; border-radius: 4px;
  margin: 0 4px 4px 0;
}
.pf-verb-chip-read { background: rgba(0,113,227,.12); color: var(--pf-accent); }
.pf-verb-chip-write { background: var(--pf-surface-2); color: var(--pf-text-muted); }
.pf-verb-chip-send { background: var(--pf-warn-tint); color: var(--pf-warn); }
.pf-verb-chip-destructive { background: var(--pf-danger-tint); color: var(--pf-danger); }
.pf-aa-row {
  background: var(--pf-surface); border: 1px solid var(--pf-border); border-radius: 8px;
  padding: 12px 14px; margin-bottom: 8px; max-width: 720px;
}
.pf-aa-row-main { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.pf-aa-sentence { font-size: 13.5px; color: var(--pf-text); flex: 1 1 300px; }
.pf-aa-verbs { flex-shrink: 0; }
.pf-aa-tools { font-size: 12px; color: var(--pf-text-muted); margin-top: 8px; line-height: 1.5; }
.pf-aa-usage { font-size: 12px; color: var(--pf-text-muted); }
.pf-aa-usage-stale { color: var(--pf-warn); font-weight: 600; }
.pf-aa-add { max-width: 620px; }
.pf-aa-add .pf-input { margin-top: 10px; width: 100%; }

/* ---- Copy-ID toast (right-click a grant row -- see data-copy-id) ---- */
.pf-copy-toast {
  position: fixed; background: #1d1d1f; color: #fff; font-size: 11px; font-weight: 500;
  padding: 4px 9px; border-radius: 5px; pointer-events: none; z-index: 999; opacity: .95;
  transform: translate(-50%, -100%);
}

/* ---- Privacy ---- */
.pf-policy-row {
  display: flex; align-items: center; justify-content: space-between; padding: 12px 14px;
  background: var(--pf-surface); border: 1px solid var(--pf-border); border-radius: 8px; margin-bottom: 14px; max-width: 560px;
}
.pf-policy-row-label { font-size: 13px; font-weight: 600; color: var(--pf-text); }
.pf-policy-row-label .pf-muted { font-weight: 400; color: var(--pf-text-dim); }
.pf-category-row {
  display: flex; align-items: center; justify-content: space-between; padding: 11px 14px;
  border-bottom: 1px solid var(--pf-border); max-width: 560px;
}
.pf-category-label { font-size: 13.5px; color: var(--pf-text); font-weight: 500; }
.pf-category-key { font-size: 11.5px; color: var(--pf-text-dim); font-family: ui-monospace, monospace; margin-top: 2px; }

/* ---- Audit ---- */
.pf-export-row { display: flex; align-items: center; gap: 14px; margin-bottom: 22px; }
.pf-export-hint { font-size: 12px; color: var(--pf-text-muted); }
.pf-audit-card { max-width: 640px; margin-bottom: 22px; }
.pf-audit-card-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.pf-audit-card-row:last-child { margin-bottom: 0; }
.pf-audit-card-title { font-size: 13.5px; font-weight: 600; color: var(--pf-text); }
.pf-audit-logfile { font-size: 12px; color: var(--pf-text-muted); font-family: ui-monospace, monospace; }
.pf-audit-list { max-width: 640px; border: 1px solid var(--pf-border); border-radius: 10px; overflow: hidden; }
.pf-audit-row {
  display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: var(--pf-content-bg);
  border-bottom: 1px solid var(--pf-border);
}
.pf-audit-row:last-child { border-bottom: none; }
.pf-audit-connector { width: 80px; font-size: 12px; color: var(--pf-text-muted); flex-shrink: 0; }
.pf-audit-tool { flex: 1; font-size: 12.5px; color: var(--pf-text); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pf-audit-badge { font-size: 10.5px; font-weight: 600; padding: 3px 8px; border-radius: 9px; flex-shrink: 0; }
.pf-audit-badge.denied { background: var(--pf-danger-tint); color: var(--pf-danger); }
.pf-audit-badge.auto_accepted { background: rgba(0,113,227,.1); color: var(--pf-accent); }
.pf-audit-badge.other { background: var(--pf-surface-2); color: var(--pf-text-muted); }
.pf-audit-time { width: 70px; text-align: right; font-size: 11.5px; color: var(--pf-text-dim); flex-shrink: 0; }

/* ---- About ---- */
.pf-about-page {
  flex: 1; overflow-y: auto; padding: 56px 44px; display: flex; flex-direction: column;
  align-items: center; text-align: center;
}
.pf-about-icon {
  width: 76px; height: 76px; border-radius: 18px; background: var(--pf-accent); color: #fff; font-size: 26px;
  font-weight: 700; display: flex; align-items: center; justify-content: center; margin-bottom: 18px;
}
.pf-about-name { font-size: 20px; font-weight: 700; color: var(--pf-text); }
.pf-about-version { font-size: 13px; color: var(--pf-text-muted); margin-top: 4px; }
.pf-about-desc { font-size: 13px; color: var(--pf-text-muted); margin-top: 18px; max-width: 420px; line-height: 1.5; }
.pf-about-repo { margin-top: 20px; font-size: 13px; color: var(--pf-accent); cursor: pointer; }
.pf-about-license { font-size: 12px; color: var(--pf-text-dim); margin-top: 6px; }
.pf-about-buttons { display: flex; gap: 12px; margin-top: 28px; }

/* ---- Telegram sign-in modal ---- */
/* Not part of the design mockup (which has no multi-step-form concept
   anywhere) -- kept visually consistent with the rest of the app (same
   fonts/colors/button styles already defined above) rather than a new
   look of its own. */
.pf-modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.35); display: flex;
  align-items: center; justify-content: center; z-index: 100;
}
.pf-modal {
  background: var(--pf-content-bg); border-radius: 12px; padding: 24px 28px; width: 360px;
  box-shadow: 0 20px 60px rgba(0,0,0,.35);
}
.pf-modal-title { font-size: 15px; font-weight: 600; color: var(--pf-text); margin-bottom: 4px; }
.pf-modal-desc { font-size: 12px; color: var(--pf-text-muted); margin-bottom: 14px; line-height: 1.4; }
.pf-modal-error { font-size: 12px; color: var(--pf-danger); margin-bottom: 10px; }
.pf-modal-input { width: 100%; margin-bottom: 16px; }
.pf-modal-buttons { display: flex; justify-content: flex-end; gap: 10px; }
"""

# ---------------------------------------------------------------------------- #
# JS -- render(state) rebuilds #app's innerHTML from the merged python+ui
# state on every call; post() is the JS->Python half of the bridge.
# ---------------------------------------------------------------------------- #

_JS = r"""
(function () {
  // PSC-5: window.__pfCapabilities is Python's own settings_window_html.
  // build_html()/_capabilities_for() -- which inner-nav sections this
  // (mode, is_admin) combination gets, and which individual actions never
  // have a real route to post to at all (org_settings_scope.
  // NOT_APPLICABLE_ACTIONS, plus the handful of local-only bespoke actions
  // that table doesn't cover -- see that function's own docstring). Every
  // call site before PSC-5 renders under the fallback below, which hides
  // nothing -- local mode's own rendering is unaffected by any of this.
  var CAPS = window.__pfCapabilities || {
    mode: 'local', is_admin: false,
    sections: { general: true, connectors: true, auto_accept: true, privacy: true, audit: true, about: true },
    not_applicable_actions: [],
  };

  function notApplicable(action) {
    return CAPS.not_applicable_actions.indexOf(action) !== -1;
  }

  var ui = {
    // issue #396 Part C: window.__pfInitialSection lets one specific route
    // (GET /settings/connectors, see web/routes_settings.py) land here with
    // Connectors already selected -- a real, server-decided initial value
    // for what's otherwise purely client-side UI state (see this module's
    // own docstring on `ui`). Every other route omits the script that sets
    // it, so this falls back to 'general' exactly as before -- except in a
    // mode where General itself is hidden (PSC-5: a non-admin org
    // principal), where landing on a nav item that isn't drawn at all would
    // leave the page with no visible selection; Auto-accept is the one
    // section every org principal, admin or not, always gets.
    section: (window.__pfInitialSection || (CAPS.sections.general ? 'general' : 'auto_accept')),
    privacyGroup: null,
    // Auto-accept page (P6) -- see renderAutoAccept below for how each is used.
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
    var cls = 'pf-toggle' + (on ? ' on' : '') + (disabled ? ' disabled' : '');
    var attrs = disabled ? '' : dataAttr(action, payload);
    return '<div class="' + cls + '" role="switch" aria-checked="' + (on ? 'true' : 'false') + '" ' +
      (disabled ? 'aria-disabled="true"' : 'tabindex="0"') +
      (ariaLabel ? ' aria-label="' + esc(ariaLabel) + '"' : '') + ' ' + attrs +
      '><div class="pf-knob"></div></div>';
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

  // -------------------------------------------------------------------- //
  // Nav
  // -------------------------------------------------------------------- //

  var NAV_ITEMS = [
    ['general', 'General'], ['connectors', 'Connectors'], ['auto_accept', 'Auto-accept'],
    ['privacy', 'Privacy Filter'], ['audit', 'Audit Log'], ['about', 'About'],
  ];

  function renderNav(state) {
    var html = '<div class="pf-nav" role="tablist" aria-label="Settings sections">';
    NAV_ITEMS.forEach(function (item) {
      var key = item[0], label = item[1];
      if (CAPS.sections[key] === false) { return; }
      var active = ui.section === key;
      html += '<div class="pf-navitem' + (active ? ' active' : '') + '" role="tab" aria-selected="' +
        (active ? 'true' : 'false') + '" tabindex="0" aria-label="' + esc(label) + '" data-nav="' + key + '">' + esc(label) + '</div>';
    });
    html += '<div class="pf-nav-spacer"></div>';
    html += '<div class="pf-nav-version">PrivacyFence ' + esc(state.about.version) + '</div>';
    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // General
  // -------------------------------------------------------------------- //

  // Approval-notification permission (docs/approval-list-ui-ux.md §4.4) is
  // browser state, not anything SettingsController tracks or this window's
  // Python side could toggle -- Notification.permission lives per-origin in
  // the browser itself, so this reads it live off `window` at render time
  // rather than off `state`. Deliberately independent of window.
  // __pfNotifPrompt's own one-shot toast (web_shell.py): that toast fires
  // once, right after a first decision, and never again once shown; this
  // card is the permanent, re-visitable home for the same action -- exactly
  // what §4.4 calls for and the toast alone can't be ("a header toggle...
  // if permission is denied, say so and link to the browser's own
  // instructions"). Granting (or denying) here also means the toast simply
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
  // web.notifications.detail (settings.yaml.example, docs/
  // approval-list-ui-ux.md §4.3) -- unlike the enabled flag above, this one
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
    // pf-audit-card-row/pf-audit-card-title -- the same stacked-row
    // treatment renderAudit's own "Log level" segmented control uses below
    // (a second row inside the same card, not a fresh pf-card-row, which
    // has no bottom-margin-between-rows rule of its own).
    var current = state.general.notifications_detail || 'minimal';
    var html = '<div class="pf-audit-card-row"><div>';
    html += '<div class="pf-audit-card-title">Detail level</div>';
    html += '<div class="pf-card-desc">' + esc(NOTIFICATIONS_DETAIL_DESCRIPTIONS[current] || '') + '</div></div>';
    html += segGroupHtml(['minimal', 'standard', 'detailed'].map(function (lvl) {
      return { label: NOTIFICATIONS_DETAIL_LABELS[lvl], active: current === lvl, action: 'set_notifications_detail', payload: { level: lvl } };
    }), 'Notification detail level');
    html += '</div>';
    return html;
  }

  function renderNotificationsCard(state) {
    var html = '<div class="pf-card pf-audit-card">';
    html += '<div class="pf-audit-card-row"><div>';
    html += '<div class="pf-card-title">Approval Notifications</div>';
    html += '<div class="pf-card-desc">A desktop notification when Claude needs your approval and this tab isn\'t focused -- from your browser, no server or push service involved.</div></div>';
    if (typeof Notification === 'undefined') {
      html += '<div class="pf-export-hint">Not supported in this window.</div>';
    } else if (window.__pfNotificationsEnabled === false) {
      html += '<div class="pf-export-hint">Turned off in configuration (web.notifications.enabled).</div>';
    } else if (Notification.permission === 'granted') {
      html += '<div class="pf-export-hint">Enabled</div>';
    } else if (Notification.permission === 'denied') {
      html += '<div class="pf-export-hint">Blocked -- allow notifications for this site in your browser\'s settings, then reload.</div>';
    } else {
      html += '<div class="pf-btn-primary" role="button" tabindex="0" aria-label="Enable notifications" data-notif-enable="1">Enable</div>';
    }
    html += '</div>';
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
    html += '<div class="pf-page-title">General</div>';
    html += renderNotificationsCard(state);

    // §16.2.4: the web surface's replacement for _show_update_available_
    // alert's native rumps.alert() -- an in-page banner whose three
    // buttons map onto the exact same three outcomes (skip this version /
    // remind me later / download), rather than a blocking native modal an
    // HTTP request has no business popping up on the daemon's machine.
    if (g.update_available) {
      html += '<div class="pf-card pf-update-banner"><div class="pf-card-row">';
      html += '<div><div class="pf-card-title">Update available</div>';
      html += '<div class="pf-card-desc">PrivacyFence ' + esc(g.update_latest_version) +
        (g.update_is_beta ? ' (beta)' : '') + ' is available (you have ' + esc(g.version) + ').</div></div>';
      html += '<div style="display:flex;gap:8px;flex-shrink:0;">';
      html += '<a class="pf-btn-secondary" style="text-decoration:none;display:inline-block" href="' +
        esc(g.update_release_url || '') + '" target="_blank" rel="noopener">Download</a>';
      html += '<div class="pf-btn-secondary" role="button" tabindex="0" aria-label="Remind me later" ' +
        dataAttr('remind_later_update', {}) + '>Remind Me Later</div>';
      html += '<div class="pf-btn-secondary" role="button" tabindex="0" aria-label="Skip this version" ' +
        dataAttr('skip_update', {}) + '>Skip</div>';
      html += '</div></div></div>';
    }

    html += '<div class="pf-card">';
    html += '<div class="pf-card-row"><div><div class="pf-card-title">PII Detection Gate</div>';
    html += '<div class="pf-card-desc">Scans review-popup content for likely personal data (IBANs, national IDs, financial figures) before you approve it. A match requires a second confirmation.</div></div>';
    html += toggleHtml(g.pii_enabled, 'toggle_pii_detection', {}, false, 'PII Detection Gate');
    html += '</div>';
    html += '<div class="pf-divider"></div>';
    html += '<div class="pf-subrow" style="opacity:' + (g.pii_enabled ? 1 : .4) + '"><div class="pf-subrow-label">Detect IP addresses</div>';
    html += toggleHtml(g.pii_ip, 'toggle_pii_category', { category_key: 'detect_ip_addresses' }, !g.pii_enabled, 'Detect IP addresses');
    html += '</div>';
    html += '<div class="pf-subrow" style="opacity:' + (g.pii_enabled ? 1 : .4) + '"><div class="pf-subrow-label">Detect financial figures</div>';
    html += toggleHtml(g.pii_financial, 'toggle_pii_category', { category_key: 'detect_financial_figures' }, !g.pii_enabled, 'Detect financial figures');
    html += '</div></div>';

    // #426 Phase 1: a plain same-origin <a>, not a data-action AJAX call --
    // /security is its own standalone page (web/routes_security.py), not
    // part of this SPA's own render() dispatch.
    html += '<div class="pf-card"><div class="pf-card-row"><div><div class="pf-card-title">Security</div>';
    html += '<div class="pf-card-desc">Manage passkeys (Face ID, Touch ID, Windows Hello) enrolled against this install.</div></div>';
    html += '<a class="pf-btn-secondary" style="text-decoration:none;display:inline-block" href="/security">Manage passkeys</a>';
    html += '</div>';
    // B9: the "turn step-up on" control -- one-directional (see
    // SettingsController.enable_step_up's own docstring for why turning
    // it back off stays a config.yaml-plus-restart operation with no
    // control here). Three states, mirroring what enable_step_up itself
    // will and won't accept: already on (nothing left to do -- a hand
    // edit is the only way back to any other state), on but no passkey
    // yet (the control would just 400/self.error -- hidden, with a hint
    // pointing at the button above instead of a control guaranteed to
    // fail), or ready (the actual button).
    // B9's own machinery hardcodes LOCAL_PRINCIPAL throughout
    // (org_settings_scope.ACTION_SCOPES's own comment on enable_step_up) --
    // stepUpApplicable is g.step_up_available further gated on this not
    // being one of the modes/principals that control could never act
    // correctly for.
    var stepUpApplicable = g.step_up_available && !notApplicable('enable_step_up');
    if (stepUpApplicable && g.step_up_on) {
      html += '<div class="pf-divider"></div><div class="pf-card-row"><div>';
      html += '<div class="pf-card-title">Step-up for approvals</div>';
      html += '<div class="pf-card-desc">On -- a write approval or a sensitive settings change demands your passkey. ' +
        'To turn this off, edit <code>step_up.require_passkey</code> in <code>config/settings.yaml</code> and restart PrivacyFence.</div>';
      html += '</div></div>';
    } else if (stepUpApplicable && !g.step_up_has_passkey) {
      html += '<div class="pf-divider"></div><div class="pf-card-row"><div>';
      html += '<div class="pf-card-title">Step-up for approvals</div>';
      html += '<div class="pf-card-desc">Off. Add a passkey above first, then come back here to require it for every write approval.</div>';
      html += '</div></div>';
    } else if (stepUpApplicable) {
      html += '<div class="pf-divider"></div><div class="pf-card-row"><div>';
      html += '<div class="pf-card-title">Step-up for approvals</div>';
      html += '<div class="pf-card-desc">Off. Require your passkey for every write approval and every sensitive settings change.</div>';
      html += '</div><div class="pf-btn-primary" role="button" tabindex="0" aria-label="Turn on step-up for approvals" ' +
        dataAttr('enable_step_up', {}) + '>Turn on</div></div>';
    }
    html += '</div>';

    // PSC-5: both cards below are meaningless on a headless org server --
    // an update check against GitHub Releases and installing *this org's
    // own* configuration bundle are both local-desktop-install concepts
    // (org_settings_scope.NOT_APPLICABLE_ACTIONS covers toggle_update_check;
    // install_org_config has no ACTION_SCOPES entry at all, since it isn't
    // one of the generic dispatcher's actions -- both are the same "hidden
    // for org" decision).
    if (!notApplicable('toggle_update_check')) {
      html += '<div class="pf-card"><div class="pf-card-row"><div><div class="pf-card-title">Check for Updates</div>';
      html += '<div class="pf-card-desc">Once-a-day check against GitHub Releases. Never installs anything automatically.</div></div>';
      html += toggleHtml(g.update_check_enabled, 'toggle_update_check', {}, false, 'Check for Updates');
      html += '</div><div class="pf-divider"></div>';
      html += '<div class="pf-subrow" style="opacity:' + (g.update_check_enabled ? 1 : .4) + '"><div class="pf-subrow-label">Receive beta releases</div>';
      html += toggleHtml(g.update_check_beta, 'toggle_update_check_beta', {}, !g.update_check_enabled, 'Receive beta releases');
      html += '</div></div>';
    }

    if (!notApplicable('install_org_config')) {
      html += '<div class="pf-card"><div class="pf-card-title">Organization Configuration</div>';
      html += '<div class="pf-card-desc" style="margin-bottom:12px;">OAuth app credentials and unattended-session policy, provided by your IT administrator.</div>';
      html += '<div style="display:flex;align-items:center;gap:14px;">';
      html += '<div class="pf-btn-primary" role="button" tabindex="0" aria-label="' + esc(g.org_button_label) + '" ' +
        dataAttr('install_org_config', {}) + '>' + esc(g.org_button_label) + '</div>';
      if (g.org_installed && g.org_installed_date) {
        html += '<div class="pf-export-hint">Installed ' + esc(g.org_installed_date) + '</div>';
      } else if (!g.org_installed) {
        html += '<div class="pf-export-hint">Not installed</div>';
      }
      html += '</div></div>';
    }

    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Connectors
  // -------------------------------------------------------------------- //

  function connectorStatus(c) {
    if (c.busy) return { text: 'Connecting…', cls: 'pf-pill-warn' };
    if (c.authed) return { text: 'Connected', cls: 'pf-pill-connected' };
    if (!c.enabled) return { text: 'Disabled', cls: 'pf-pill-neutral' };
    if (!c.has_org) {
      return { text: c.key === 'telegram' ? 'App credentials missing' : 'Organization config missing', cls: 'pf-pill-missing' };
    }
    return { text: 'Not connected', cls: 'pf-pill-neutral' };
  }

  // issue #396 Part C: shown on first run (nothing authenticated yet) so
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
    var html = '<div class="pf-card pf-welcome-banner"><div class="pf-card-row">';
    html += '<div><div class="pf-card-title">Welcome to PrivacyFence</div>';
    html += '<div class="pf-card-desc">PrivacyFence is a privacy and approval gateway between Claude and your ' +
      'real accounts (Gmail, Drive, Slack, and similar) -- it governs access to them, it does not provide them ' +
      'itself. Nothing is governed until at least one connector below is authenticated. If your IT team gave ' +
      'you an organization config bundle, install it first from the General page; otherwise authenticate a ' +
      'connector directly below, then go back to Claude.</div></div>';
    html += '<div class="pf-welcome-dismiss" role="button" tabindex="0" aria-label="Dismiss welcome message" ' +
      'data-dismiss-welcome="1">✕</div>';
    html += '</div></div>';
    return html;
  }

  function renderConnectors(state) {
    var html = '<div class="pf-page">';
    html += '<div class="pf-page-title">Connectors</div>';
    html += '<div class="pf-page-subtitle">Authenticate a connector to let Claude access it, subject to approval and policy. ' +
      'Signing in opens a browser window on the machine running PrivacyFence -- not necessarily this device.</div>';
    html += renderWelcomeBanner(state);
    state.connectors.forEach(function (c) {
      var status = connectorStatus(c);
      html += '<div class="pf-connector-row">';
      html += '<div class="pf-connector-icon">' + (c.icon_data_uri ? '<img src="' + esc(c.icon_data_uri) + '" alt="' + esc(c.label) + '"/>' : '') + '</div>';
      html += '<div class="pf-connector-label">' + esc(c.label) + '</div>';
      html += '<div class="pf-pill ' + status.cls + '">' + esc(status.text) + '</div>';
      html += '<div class="pf-spacer"></div>';
      var authDisabled = c.busy;
      if (c.key === 'telegram') {
        // Telegram's phone/code/2FA flow needs its own multi-step modal
        // (see renderTelegramModal below) instead of the generic single-
        // click OAuth flow every other connector uses -- intercepted here
        // client-side (data-telegram-auth, not data-action) so opening the
        // modal at the phone-entry step needs no round trip to Python;
        // the first real bridge call is telegram_start_auth() once a
        // phone number is actually submitted.
        html += '<div class="pf-auth-link' + (authDisabled ? ' disabled' : '') +
          '" role="button" tabindex="0" aria-label="' + esc(c.auth_label) + ' Telegram"' +
          (authDisabled ? '' : ' data-telegram-auth="1"') + '>' + esc(c.auth_label) + '</div>';
      } else {
        html += '<div class="pf-auth-link' + (authDisabled ? ' disabled' : '') +
          '" role="button" tabindex="0" aria-label="' + esc(c.auth_label) + ' ' + esc(c.label) + '" ' +
          (authDisabled ? '' : dataAttr('authenticate_connector', { connector: c.key })) + '>' + esc(c.auth_label) + '</div>';
      }
      // F6 of the self-approval review: directional, not a single
      // toggle_connector -- re-enabling a connector is gated
      // (_SENSITIVE_ACTIONS) differently from disabling one (see
      // web/routes_settings.py's own classification comment), so the
      // action this click sends has to match which direction the very
      // next click will actually take.
      html += toggleHtml(c.enabled, c.enabled ? 'disable_connector' : 'enable_connector', { connector: c.key }, false, c.label + ' enabled');
      html += '</div>';
    });
    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Auto-accept (policy v2) -- P6 of the policy v2 redesign. One filterable
  // rule list, sentence-rendered server-side (policy.describe), replacing
  // the old per-connector Trusted-*/parallel-rule-row/Sheets-Docs-pointer-
  // page surface this file used to carry (see git history for renderRules'
  // pre-P6 shape). Every "Add rule" submission writes straight to the v2
  // auto_accept: section (settings_controller.add_policy_rule) -- there is
  // no rule_type dropdown here the way the old per-operation rows had one,
  // because a scope's own verb checkboxes (aa.scope_groups[].verbs) are
  // what a v2 rule is actually keyed on, not a v1 rule name.
  //
  // P8 adds each row's own usage line (settings_controller._auto_accept_state, from
  // AuditLogger.rule_usage()) -- "Matched Nx, last <when>" for a rule that has actually let
  // something through, or a distinct stale badge next to the (already-existing) Remove link
  // for one that never has, resolving F9's "which of my rules have never matched" question.
  // -------------------------------------------------------------------- //

  function verbChipsHtml(verbs) {
    return verbs.map(function (v) {
      return '<span class="pf-verb-chip pf-verb-chip-' + esc(v.family) + '">' + esc(v.verb) + '</span>';
    }).join('');
  }

  function renderAutoAccept(state) {
    var aa = state.auto_accept;
    var search = (ui.aaSearch || '').trim().toLowerCase();
    var connFilter = ui.aaConnectorFilter || [];
    var famFilter = ui.aaFamilyFilter || [];
    var expanded = ui.aaExpanded || {};

    var html = '<div class="pf-page">';
    html += '<div class="pf-page-title">Auto-accept</div>';
    html += '<div class="pf-page-subtitle">Every standing rule that lets Claude act without asking first, across every connector -- what a rule actually unblocks is shown before you add it, and again on its own row below.</div>';

    html += '<div class="pf-aa-filterbar">';
    html += '<input type="text" class="pf-input pf-aa-search" placeholder="Filter by connector, tool, or value…" aria-label="Filter rules" value="' +
      esc(ui.aaSearch) + '" data-aa-search="1"/>';
    aa.connectors.forEach(function (cname) {
      var active = connFilter.indexOf(cname) !== -1;
      html += '<div class="pf-fchip' + (active ? ' active' : '') + '" role="button" tabindex="0" aria-pressed="' +
        (active ? 'true' : 'false') + '" aria-label="Filter to ' + esc(cname) + '" data-aa-connector-filter="' +
        esc(cname) + '">' + esc(cname) + '</div>';
    });
    ['read', 'write', 'send', 'destructive'].forEach(function (fam) {
      var active = famFilter.indexOf(fam) !== -1;
      html += '<div class="pf-fchip pf-verb-chip-' + fam + (active ? ' active' : '') + '" role="button" tabindex="0" aria-pressed="' +
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
      html += '<div class="pf-rules-empty">' +
        (aa.rules.length ? 'No rules match this filter.' : 'No auto-accept rules configured yet -- add one below.') +
        '</div>';
    }
    rows.forEach(function (r) {
      var isExpanded = !!expanded[r.id];
      var copyAttr = r.value_ids.length ? ' data-copy-id="' + esc(r.value_ids.join(', ')) +
        '" title="' + esc(r.value_ids.join(', ')) + ' -- right-click to copy"' : '';
      html += '<div class="pf-aa-row"' + copyAttr + '>';
      html += '<div class="pf-aa-row-main"><div class="pf-aa-sentence">' + esc(r.sentence) + '</div>';
      html += '<div class="pf-aa-verbs">' + verbChipsHtml(r.verbs) + '</div></div>';
      html += '<div style="display:flex;align-items:center;gap:14px;margin-top:6px;">';
      html += '<div class="pf-link" role="button" tabindex="0" aria-expanded="' + (isExpanded ? 'true' : 'false') +
        '" aria-label="What this unblocks" data-aa-expand="' + esc(r.id) + '">' + (isExpanded ? '▾' : '▸') +
        ' Unblocks ' + r.covered_tools.length + ' tool' + (r.covered_tools.length === 1 ? '' : 's') + '</div>';
      html += '<div class="pf-link-danger" role="button" tabindex="0" aria-label="Remove rule" ' +
        dataAttr('remove_policy_rule', { rule_id: r.id }) + '>✕ Remove</div>';
      html += '</div>';
      // P8: per-rule usage -- "Matched 42x, last 3 days ago" or, for a rule that has never
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

    html += '<div class="pf-card pf-aa-add"><div class="pf-card-title">Add a rule</div>';
    html += '<select class="pf-input" aria-label="Scope" data-aa-group-select="1">';
    aa.scope_groups.forEach(function (g) {
      html += '<option value="' + esc(g.id) + '"' + (g.id === ui.aaGroup ? ' selected' : '') + '>' + esc(g.label) + '</option>';
    });
    html += '</select>';
    if (group && group.needs_value) {
      html += '<input type="text" class="pf-input pf-input-mono" placeholder="' + esc(group.value_hint) +
        '" aria-label="Value (comma-separated for more than one)" value="' + esc(ui.aaValue) + '" data-aa-value="1"/>';
    }
    if (group) {
      html += '<div class="pf-caps-row">';
      group.verbs.forEach(function (verb) {
        var on = !!checkedVerbs[verb];
        html += '<div class="pf-cap-chip' + (on ? ' on' : '') + '" role="checkbox" aria-checked="' + (on ? 'true' : 'false') +
          '" tabindex="0" aria-label="' + esc(verb) + '" data-aa-verb-toggle="' + esc(verb) + '">' + esc(verb) + '</div>';
      });
      html += '</div>';
    }
    html += '<div class="pf-btn-primary" role="button" tabindex="0" aria-label="Add rule" data-aa-add="1" ' +
      'style="margin-top:12px;display:inline-block;">Add rule</div>';
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

    var html = '<div class="pf-subnav" role="tablist" aria-label="Privacy Filter group">';
    privacy.groups.forEach(function (pg) {
      var active = ui.privacyGroup === pg.key;
      html += '<div class="pf-subnav-item' + (active ? ' active' : '') + '" role="tab" aria-selected="' +
        (active ? 'true' : 'false') + '" tabindex="0" aria-label="' + esc(pg.label) +
        '" data-privacy-nav="' + esc(pg.key) + '"><span>' + esc(pg.label) + '</span></div>';
    });
    html += '</div>';

    html += '<div class="pf-detail-page">';
    if (ui.privacyGroup === 'calendar') {
      html += '<div class="pf-detail-title">Calendar</div>';
      html += '<div class="pf-detail-subtitle">Calendar has no category schema — this is its one privacy-relevant setting.</div>';
      html += '<div class="pf-card" style="max-width:560px;"><div class="pf-card-row"><div>';
      html += '<div class="pf-card-title" style="font-size:13.5px;">Show full event details in free/busy</div>';
      html += '<div class="pf-card-desc">When off, calendar_get_free_busy always returns busy/free blocks only, never titles or status, regardless of access.</div>';
      html += '</div>' + toggleHtml(privacy.calendar_free_busy, 'toggle_calendar_free_busy', {}, false, 'Show full event details in free/busy') + '</div></div>';
    } else {
      var group = ui.privacyGroup;
      var label = '';
      privacy.groups.forEach(function (pg) { if (pg.key === group) label = pg.label; });
      var defaultPolicy = privacy.default_policy[group];
      var categories = privacy.categories[group] || [];

      html += '<div class="pf-detail-title">' + esc(label) + '</div>';
      html += '<div class="pf-detail-subtitle">Applied before this data reaches the review popup, Claude, or the audit log — a floor under human review, not a substitute for it.</div>';

      html += '<div class="pf-policy-row"><div class="pf-policy-row-label">Default policy <span class="pf-muted">(unlisted categories)</span></div>';
      html += policySegHtml(defaultPolicy, 'set_default_policy', { group: group }, 'Default policy');
      html += '</div>';

      categories.forEach(function (cat) {
        html += '<div class="pf-category-row"><div><div class="pf-category-label">' + esc(cat.label) + '</div>';
        html += '<div class="pf-category-key">' + esc(cat.key) + '</div></div>';
        html += policySegHtml(cat.policy, 'set_category_policy', { group: group, category: cat.key }, cat.label + ' policy');
        html += '</div>';
      });

      if (group === 'privacy' && typeof privacy.gmail_append_signature === 'boolean') {
        html += '<div class="pf-card" style="max-width:560px;margin-top:16px;"><div class="pf-card-row"><div>';
        html += '<div class="pf-card-title" style="font-size:13.5px;">Append Gmail signature to drafts</div>';
        html += '<div class="pf-card-desc">Adds your Gmail signature to the end of every draft Claude creates, unless a call says otherwise. It is shown in the approval popup with the rest of the draft.</div>';
        html += '</div>' + toggleHtml(privacy.gmail_append_signature, 'toggle_gmail_signature', {}, false, 'Append Gmail signature to drafts') + '</div></div>';
      }
    }
    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Audit
  // -------------------------------------------------------------------- //

  var LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

  function renderAudit(state) {
    var audit = state.audit;
    var html = '<div class="pf-page">';
    html += '<div class="pf-page-title">Audit Log</div>';
    html += '<div class="pf-page-subtitle">Every decision — accepted, denied, or auto-accepted — is recorded locally as JSON lines, then exported weekly to a formatted Excel workbook.</div>';

    html += '<div class="pf-export-row"><div class="pf-btn-primary" role="button" tabindex="0" aria-label="Export Audit Log" ' +
      dataAttr('export_audit_log', {}) + '>Export Audit Log…</div>';
    html += '<div class="pf-export-hint">' + esc(audit.export_hint) + '</div></div>';

    html += '<div class="pf-card pf-audit-card">';
    html += '<div class="pf-audit-card-row"><div class="pf-audit-card-title">Log level</div>';
    html += segGroupHtml(LOG_LEVELS.map(function (lvl) {
      return { label: lvl, active: audit.log_level === lvl, action: 'set_log_level', payload: { level: lvl } };
    }), 'Log level');
    html += '</div>';
    html += '<div class="pf-audit-card-row"><div class="pf-audit-card-title">Log file</div>';
    html += '<div class="pf-audit-logfile">' + esc(audit.log_file) + '</div></div>';
    html += '</div>';

    html += '<div class="pf-group-title">Recent decisions</div>';
    html += '<div class="pf-audit-list">';
    if (audit.recent.length === 0) {
      html += '<div class="pf-audit-row"><div class="pf-audit-tool" style="color:#8a8a8e;">Nothing logged yet.</div></div>';
    }
    audit.recent.forEach(function (a) {
      var badgeCls = a.decision === 'denied' || a.decision === 'rejected' ? 'denied' : (a.decision === 'auto_accepted' ? 'auto_accepted' : 'other');
      html += '<div class="pf-audit-row">';
      html += '<div class="pf-audit-connector">' + esc(a.connector) + '</div>';
      html += '<div class="pf-audit-tool">' + esc(a.tool) + '</div>';
      html += '<div class="pf-audit-badge ' + badgeCls + '">' + esc(a.decision) + '</div>';
      html += '<div class="pf-audit-time">' + esc(a.time) + '</div>';
      html += '</div>';
    });
    html += '</div></div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // About
  // -------------------------------------------------------------------- //

  function renderAbout(state) {
    var about = state.about;
    var html = '<div class="pf-about-page">';
    html += '<div class="pf-about-icon">PF</div>';
    html += '<div class="pf-about-name">PrivacyFence</div>';
    html += '<div class="pf-about-version">Version ' + esc(about.version) + '</div>';
    html += '<div class="pf-about-desc">Human control and policy enforcement for AI access to enterprise data.</div>';
    html += '<div class="pf-about-repo" role="button" tabindex="0" aria-label="Open GitHub repository" ' +
      dataAttr('open_repo', {}) + '>' + esc(about.repo_url.replace('https://', '')) + ' ↗</div>';
    html += '<div class="pf-about-license">' + esc(about.license) + '</div>';
    html += '<div class="pf-about-buttons">';
    // check_for_updates/quit_app: a version-update check against GitHub
    // Releases and shutting down the daemon are both local-install
    // concepts -- neither has an org route (org's own routes_settings.py
    // never mounts either), so both are unconditionally in this
    // capability set's not_applicable_actions for org mode.
    if (!notApplicable('check_for_updates')) {
      html += '<div class="pf-btn-secondary" role="button" tabindex="0" aria-label="Check for Updates" ' +
        dataAttr('check_for_updates', {}) + '>Check for Updates</div>';
    }
    if (!notApplicable('quit_app')) {
      html += '<div class="pf-btn-danger" role="button" tabindex="0" aria-label="Quit PrivacyFence" ' +
        dataAttr('quit_app', {}) + '>Quit PrivacyFence</div>';
    }
    html += '</div></div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Top-level render
  // -------------------------------------------------------------------- //

  function renderSection(state) {
    // PSC-5: a section CAPS itself hides never renders, even if `ui.section`
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
    html += '<input type="' + copy.type + '" class="pf-input pf-modal-input" placeholder="' + esc(copy.placeholder) +
      '" data-telegram-field="' + copy.field + '" aria-label="' + esc(copy.placeholder) + '"' + (busy ? ' disabled' : '') + '/>';
    html += '<div class="pf-modal-buttons">';
    html += '<div class="pf-btn-secondary" role="button" tabindex="0" aria-label="Cancel Telegram sign-in" data-telegram-cancel="1">Cancel</div>';
    html += '<div class="pf-btn-primary" role="button" tabindex="0" aria-label="' + esc(copy.submitLabel) +
      '" data-telegram-submit="' + copy.submitAction + '"' + (busy ? ' style="opacity:.5;pointer-events:none;"' : '') + '>' +
      (busy ? 'Working…' : esc(copy.submitLabel)) + '</div>';
    html += '</div></div></div>';
    return html;
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
    var html = renderNav(state);
    if (state.error) {
      html += '<div class="pf-content" style="flex-direction:column;">' +
        '<div class="pf-error-banner" role="alert"><div>' + esc(state.error) + '</div>' +
        '<div class="pf-error-dismiss" role="button" tabindex="0" aria-label="Dismiss error" data-dismiss-error="1">✕</div></div>' +
        '<div style="flex:1;display:flex;overflow:hidden;">' + renderSection(state) + '</div></div>';
    } else {
      html += '<div class="pf-content">' + renderSection(state) + '</div>';
    }
    html += renderTelegramModal(state);
    document.getElementById('app').innerHTML = html;
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


  // The Auto-accept page's own "Add rule" submit (P6) -- reads the current form state straight off
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

  // No blur-commit fields left as of P6 (the Auto-accept page's own value field commits live via
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


_ALL_SECTIONS = ("general", "connectors", "auto_accept", "privacy", "audit", "about")

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
    """PSC-5: which of the inner-nav sections this ``(mode, is_admin)``
    combination gets, and which individual controls (see ``dataAttr``/
    ``toggleHtml`` call sites throughout ``_JS``) must never draw at all --
    kept out of ``state`` itself (a separate ``window.__pfCapabilities``
    global, see ``build_html``) so ``state`` stays exactly what the caller
    passed in, byte for byte, the same invariant
    test_settings_window_html.py's own ``test_state_round_trips_byte_for_
    byte`` already checks.

    Local mode (every existing caller) gets every section and no
    suppressed action -- this function must be a no-op for ``mode !=
    ORG_MODE``, since that's what keeps every currently-shipped local
    rendering path (webview and web alike) unchanged by this phase.

    Org mode gets ``org_settings_scope.NOT_APPLICABLE_ACTIONS`` (every
    action with no real org route at all -- see that module for the
    single source of truth) plus two section-level decisions this
    function itself owns, both narrower than "has an org route": Connectors
    and Audit Log have no applicable action *and* nothing else worth
    showing (no read-only connector list or audit history exists for org
    mode today -- see this phase's own PR description), so the whole
    section is hidden rather than rendered empty; General and Privacy
    Filter are further gated on ``is_admin`` -- the admin-only privacy/PII
    split #400 established and this phase keeps (every action either page
    can post is itself ``admin_only`` in ``ACTION_SCOPES``, so a non-admin
    who somehow reached one would have every mutation 403 anyway; hiding
    the page is the same authorization decision, applied to rendering).
    """
    if mode != ORG_MODE:
        return {
            "mode": LOCAL_MODE, "is_admin": False,
            "sections": dict.fromkeys(_ALL_SECTIONS, True),
            "not_applicable_actions": [],
        }
    return {
        "mode": ORG_MODE, "is_admin": is_admin,
        "sections": {
            "general": is_admin, "connectors": False, "auto_accept": True,
            "privacy": is_admin, "audit": False, "about": True,
        },
        "not_applicable_actions": sorted(NOT_APPLICABLE_ACTIONS | _LOCAL_ONLY_BESPOKE_ACTIONS),
    }


def build_html(
    state: dict, *, nonce: str | None = None, initial_section: str | None = None,
    mode: str = LOCAL_MODE, is_admin: bool = False,
) -> str:
    """Full self-contained HTML document for the settings window's WKWebView
    (``mode="local"``, every caller before PSC-5) *and*, since PSC-5, for
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

    ``initial_section`` (issue #396 Part C): a deliberate, narrow exception
    to ``ui.section`` otherwise being purely client-side state (see this
    module's own docstring) -- ``GET /settings/connectors`` passes
    ``"connectors"`` so a link opened while un-onboarded lands
    directly on the screen that unblocks the user, instead of ``/settings``'s
    default General page. ``None`` (every other route) emits no script at
    all, leaving the JS's own ``'general'`` fallback exactly as before.
    PSC-5's own org-mode callers pass ``"auto_accept"``/``"privacy"`` for
    the same reason -- org mode's own General page is empty (hidden
    entirely, in fact) for a non-admin principal, so falling back to it
    would land every non-admin on a blank nav selection.

    ``mode``/``is_admin`` (PSC-5): see ``_capabilities_for`` above for
    exactly what each combination hides. Both default to local mode's own
    values, so every pre-PSC-5 call site (every one of them, until
    web/routes_settings.py's org routes started passing ``mode="org"``)
    is unaffected.
    """
    nonce = nonce or secrets.token_urlsafe(18)
    state_json = json.dumps(state)
    caps_json = json.dumps(_capabilities_for(mode, is_admin=is_admin))
    section_script = ""
    if initial_section is not None:
        section_script = f'<script nonce="{nonce}">window.__pfInitialSection = {json.dumps(initial_section)};</script>'
    return (
        "<title>PrivacyFence Settings</title>"
        f'<style nonce="{nonce}">{_TOKENS_CSS}{_CSS}</style>'
        '<div id="app"></div>'
        f'<script nonce="{nonce}">window.__pfInitialState = {state_json};</script>'
        f'<script nonce="{nonce}">window.__pfCapabilities = {caps_json};</script>'
        f"{section_script}"
        f'<script nonce="{nonce}">{_JS}</script>'
    )
