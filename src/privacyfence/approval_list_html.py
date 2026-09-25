"""Pure-function HTML for the ``/approvals`` list page
(5deef1d8:docs/approval-list-ui-ux.md §2, the P1-compatible slice its own §6 says
can land ahead of P3's full design -- the row shape, the empty state, and
the central asymmetry of §2.2: **Deny is on the row; Allow is never on the
row.** Denying without reading the card cannot leak anything; approving
from a one-line summary is exactly the habituation failure the card exists
to prevent, so there is no "Allow" button here at all -- only "Review",
which opens the real card.

Within the action cluster the order is Details, Review, Deny -- Deny last,
not adjacent to Review. Row controls are a 44px target at phone widths but
only ~30px above them, and Deny resolves an approval outright with no undo
path anywhere in the flow; putting the destructive control at the far end
of the cluster rather than one 8px gap from the safe one is the cheapest
guard against a mis-tap deciding it. This is a source-order change, not a
CSS ``order`` one, specifically so focus order and visual order stay the
same thing at every width.

``build_list_html(rows)`` is the first paint (web/routes_approvals.py, given
``approvals.PendingApproval`` objects, each with a real connector icon --
see ``row_from_approval`` below); ``window.__pfRenderApprovals(state)`` is
the live re-render web_shell.py's SSE dispatch calls with
web/state_stream.py's own "approvals" event payload
(``PendingApproval.to_summary_dict()``, which carries no icon -- these are
~15-135KB PNGs and base64'ing them into every tick would cost far more
than it buys). The icon instead rides along once, as ``_icon_connectors()``'s
``{connector: data URI}`` baked into this page's own JS and CSS at first
paint -- for *every* connector this build bundles an icon for
(``approval_icons.all_connector_icons()``), not just the ones with
something pending at that moment, so a connector with nothing pending at
load still draws its real mark the moment a row for it arrives live rather
than degrading to a letter badge until the next full page load (issue
#576). The bundled set is small and fixed (~10 files today), so this costs
one bounded, one-time addition to every first paint regardless of how many
rows are pending -- the SSE tick itself still carries no image data at
all, which is the cost this design was actually protecting against.

**The approval binder (Phase 1 of the batch-decide plan):** a sequential
agent can leave several approvals pending at once (P3's own removal of
gate.py's ``_popup_lock``), and reviewing each with its own passkey ceremony
(#426) is the regression this phase's own selection/grouping/deny-selected
work starts to close -- see approvals.PendingApproval.is_batchable()'s own
docstring for exactly which approvals that covers. This phase ships:

- a checkbox per **batchable** row (``kind == "card"``, not PII-forced --
  see ``is_batchable()``), grouped by ``(connector, operation_key)`` with a
  per-group and a page-level select-all;
- **Deny-selected**, client-side over the existing per-id decide endpoint --
  denying leaks nothing and needs no step-up, so there is no new server
  endpoint yet (that's Phase 2's batch decide endpoint);
- an inline "Details" disclosure sourced from ``GET /api/approvals/{id}/
  preview`` (the ``preview`` dict gate.py stamped at registration --
  metadata only, docs/coding-and-testing-guidelines.md §1.5), rendered with
  ``textContent`` -- never an ``<iframe>`` onto the real card document (see
  the binder plan's own "Rejected alternatives": card documents ship
  ``frame-ancestors 'none'`` and this page's own ``frame-src`` admits
  ``data:`` only).

Approving still opens the card through Phase 2 -- 5deef1d8:docs/approval-list-ui-ux.md's
own claim ("no Allow on the list") stays literally true up to there.

**Phase 3 of the binder plan** is what finally breaks that claim: an
**Approve selected** button posts the same set to the batch decide
endpoint (Phase 2) with ``result: "accept"`` on every item, gated
server-side on one WebAuthn assertion bound to the exact submitted set
(webauthn_stepup.batch_decision_fingerprint) whenever step-up applies.
``runBatch`` mirrors web/routes_settings.py's own ``pfSettingsPost``
428/403 handling almost exactly: a ``428`` carries fresh
``webauthn_options`` (and this page's own ``batch_id`` to echo back) to
complete with ``window.pfWebauthnGet`` (injected by web/routes_approvals.py's
list route, same as the settings page) and
resubmit; a ``403``/``400`` surfaces via ``window.alert`` since this page
stays open across the ceremony, unlike a one-shot card. The submit
button's own label names the selected set's composition -- "Approve 12 ·
9 reads, 3 writes" (Q1 of the binder plan's own open questions) -- so an
unintended write can't hide inside a read-shaped batch.

Selection lives in a JS ``Set`` keyed by approval id (``pfSelected``,
module-scoped closure state) and survives ``window.__pfRenderApprovals``'s
own wholesale ``innerHTML`` replace on every SSE tick: ``render()`` below
reconciles the ``Set`` against whatever ids are still present after each
render, and re-applies ``checked`` to whichever checkboxes survive. Grouping
is computed identically in Python (``_group_rows``, first paint) and JS
(``groupRows``, live re-render) -- the same duplication ``_row_html``/
``rowHtml`` already accept, so an SSE payload field this module doesn't use
yet doesn't need adding here too.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from html import escape as _html_escape
from typing import Any

from . import agent_label, approval_icons
from .agent_identity import UNKNOWN_AGENT, UNRECOGNISED_LABEL
from .agent_label import NOT_VERIFIED, TIER_ATTESTED, TIER_CLAIMED, TIER_UNKNOWN, UNKNOWN_AGENT_LABEL

_AGENT_TIERS = (TIER_ATTESTED, TIER_CLAIMED, TIER_UNKNOWN)

_EMPTY_STATE = (
    '<div class="pf-approvals-empty">'
    '<div class="pf-approvals-empty-title">Nothing is waiting.</div>'
    '<div class="pf-approvals-empty-sub">PrivacyFence is watching.</div>'
    "</div>"
)

# The same state, on an install where nothing is authenticated yet. The
# copy above is exactly right on a working install and actively misleading
# on this one: nothing is waiting because nothing *can* wait, and
# "PrivacyFence is watching" claims a protection that isn't running. The
# wording deliberately echoes settings_window_html.py's own
# renderWelcomeBanner ("Nothing is governed until at least one connector
# below is authenticated"), which is the only other place this state is
# explained today -- and which the approvals page, a landing surface in its
# own right, had no equivalent of.
_EMPTY_STATE_NOTHING_AUTHED = (
    '<div class="pf-approvals-empty">'
    '<div class="pf-approvals-empty-title">Nothing is governed yet.</div>'
    '<div class="pf-approvals-empty-sub pf-approvals-empty-body">'
    "PrivacyFence sits between Claude and your real accounts. Until a connector is "
    "authenticated, there is nothing for it to hold back."
    "</div>"
    '<a class="pf-approvals-empty-cta" href="/settings/connectors">Authenticate a connector</a>'
    "</div>"
)


def _empty_state_html(*, any_authed: bool) -> str:
    return _EMPTY_STATE if any_authed else _EMPTY_STATE_NOTHING_AUTHED

_CSS = """
.pf-approvals-page { max-width: 720px; margin: 0 auto; padding: 24px 20px 60px; width: 100%; }
.pf-approvals-heading {
  display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 16px;
}
.pf-approvals-heading:empty { margin-bottom: 0; }
.pf-approvals-count { font-size: 20px; font-weight: 600; letter-spacing: -0.01em; color: var(--color-text); }
.pf-approvals-composition { font-size: 12.5px; color: var(--color-neutral-600); }
.pf-approvals-empty {
  text-align: center; padding: 80px 20px; color: var(--color-neutral-600);
}
.pf-approvals-empty-title { font-size: 16px; font-weight: 600; color: var(--color-text); margin-bottom: 4px; }
.pf-approvals-empty-sub { font-size: 13px; }
.pf-approvals-empty-body { line-height: 1.6; max-width: 330px; margin: 0 auto 18px; }
.pf-approvals-empty-cta {
  display: inline-block; font-size: 12.5px; font-weight: 600; padding: 9px 14px;
  border-radius: var(--radius-md); background: var(--color-accent); color: #fff; text-decoration: none;
}
.pf-approvals-toolbar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 14px; margin-bottom: 12px; background: var(--color-surface); border-radius: var(--radius-lg);
}
/* An attribute selector, needed only because the rule above sets its own
   `display` unconditionally: author-origin CSS always wins over the
   user-agent stylesheet's own `[hidden] { display: none }` for a normal
   declaration, regardless of selector specificity, so without this the
   bare `hidden` attribute (see build_list_html/updateToolbar) would do
   nothing at all. */
.pf-approvals-toolbar[hidden] { display: none; }
.pf-select-all { display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
.pf-selected-count { font-size: 12.5px; color: var(--color-neutral-600); flex: 1; min-width: 0; }
.pf-btn-deny-selected {
  font-size: 12.5px; font-weight: 600; padding: 7px 12px; border-radius: var(--radius-md);
  border: 1px solid var(--color-divider); background: transparent; color: var(--color-danger); cursor: pointer;
}
.pf-btn-deny-selected:disabled { opacity: 0.5; cursor: default; }
/* Outline, not filled. Review is the one filled control on this page and
   it is the one that opens disclosure; approving a whole queue off
   one-line summaries is the habituation failure the card exists to
   prevent, so the least-informed action must not also be the loudest. The
   composition label on it ("Approve 12 · 9 reads, 3 writes") stays -- that
   part is the guard, not the problem. */
.pf-btn-approve-selected {
  font-size: 12.5px; font-weight: 600; padding: 7px 12px; border-radius: var(--radius-md);
  border: 1px solid var(--color-accent); background: transparent;
  color: var(--color-accent-700); cursor: pointer;
}
.pf-btn-approve-selected:disabled { opacity: 0.5; cursor: default; }
.pf-approval-group { margin-bottom: 14px; }
.pf-approval-group-header {
  display: flex; align-items: center; gap: 8px; padding: 6px 4px; font-size: 12.5px;
  font-weight: 600; color: var(--color-neutral-600);
}
.pf-approval-group-header label { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.pf-approval-row {
  display: flex; align-items: flex-start; gap: 12px; padding: 14px 16px;
  background: var(--color-surface); border-radius: var(--radius-lg); margin-bottom: 10px; flex-wrap: wrap;
}
/* The row is two lines now (meta above, object below), so its controls
   align to the top of the text block rather than to its centre. */
.pf-approval-row > input[type="checkbox"] { margin-top: 5px; }
.pf-approval-actions { margin-top: 1px; }
.pf-approval-icon {
  width: 28px; height: 28px; border-radius: var(--radius-md); flex-shrink: 0; object-fit: contain;
  background: var(--color-neutral-200);
}
.pf-approval-icon-fallback {
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; color: var(--color-neutral-600);
}
.pf-approval-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
/* Meta above, object below -- the row's headline is what is being touched,
   not which tool touches it. See _row_html. */
.pf-approval-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pf-approval-title {
  font-size: 15px; font-weight: 600; color: var(--color-text); line-height: 1.35;
  /* Clamped rather than free-flowing: a summary is short by construction
     (see gate.py's call sites) but nothing enforces it, and an unbounded
     title would let one row push the rest of the queue off screen. */
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; text-overflow: ellipsis;
}
.pf-approval-kicker { font-size: 12px; color: var(--color-neutral-600); }
/* Who is asking -- the card header's .pf-agent in miniature, with the same
   tier rule (ADR 0006 decision 4): only an attested row draws the vendor's
   mark; a claimed or unknown one gets a dashed "?" and "not verified". */
.pf-approval-agent {
  display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--color-neutral-600);
  min-width: 0; overflow-wrap: anywhere;
}
.pf-approval-agent-mark {
  width: 16px; height: 16px; flex-shrink: 0; box-sizing: border-box; border-radius: 4px;
  background-color: #fff; background-size: 12px 12px; background-repeat: no-repeat; background-position: center;
  box-shadow: 0 0 0 1px var(--color-divider);
}
.pf-approval-agent-glyph {
  display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px;
  flex-shrink: 0; box-sizing: border-box; border-radius: 4px; border: 1px dashed var(--color-neutral-500);
  font: 600 10px ui-monospace, Menlo, monospace;
}
.pf-approval-agent-attested .pf-approval-agent-name { font-weight: 600; color: var(--color-text); }
.pf-approval-agent-unverified { font-style: italic; }
/* Read/write direction, from gate_kind -- the same two token families and
   the same wording as the card's own .pf-pill, so a row and the card it
   opens agree on sight. Both pairs invert in tokens.css's dark block. */
.pf-approval-pill {
  font: 600 10px ui-monospace, Menlo, monospace; letter-spacing: 0.05em;
  text-transform: uppercase; padding: 2px 8px; border-radius: 20px; flex-shrink: 0;
}
.pf-approval-pill-read { background: var(--color-accent-100); color: var(--color-accent-700); }
.pf-approval-pill-write { background: var(--color-accent-2-100); color: var(--color-accent-2-700); }
.pf-approval-blocked-reason { font-size: 11.5px; color: var(--color-neutral-600); margin-top: 4px; font-style: italic; }
.pf-approval-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.pf-btn-deny, .pf-btn-review, .pf-btn-details {
  font-size: 12.5px; font-weight: 600; padding: 7px 12px; border-radius: var(--radius-md);
  border: none; cursor: pointer; text-decoration: none; white-space: nowrap;
}
.pf-btn-deny { background: transparent; color: var(--color-danger); border: 1px solid var(--color-divider); }
.pf-btn-review { background: var(--color-accent); color: #fff; }
.pf-btn-details { background: transparent; color: var(--color-text); border: 1px solid var(--color-divider); }
.pf-approval-details {
  flex-basis: 100%; font-size: 12.5px; color: var(--color-neutral-600);
  border-top: 1px solid var(--color-divider); margin-top: 8px; padding-top: 8px;
}
.pf-approval-details-row { display: flex; gap: 6px; }
.pf-approval-details-row + .pf-approval-details-row { margin-top: 3px; }
.pf-approval-details-key { font-weight: 600; color: var(--color-text); }

/* — phone widths — the row's own flex-wrap never engages on its own:
   .pf-approval-actions is flex-shrink:0 and holds ~220px of buttons, while
   .pf-approval-main is flex:1;min-width:0, so the text column can legally
   shrink to zero and does. At 393px the title truncates after two or three
   characters and the kicker goes with it. Giving the text column a basis
   too wide to sit beside the actions is what makes the wrap actually fire,
   which turns the row into what it should have been: identity on top, a
   full-width action strip underneath. */
@media (max-width: 560px) {
  .pf-approval-row { align-items: flex-start; row-gap: 0; }
  .pf-approval-main { flex-basis: calc(100% - 96px); }
  .pf-approval-actions { width: 100%; gap: 10px; margin-top: 12px; }
  /* Review takes the remaining width; Deny and Details stay at their own
     intrinsic size, so the destructive control is never the easiest one to
     hit with a thumb. */
  .pf-btn-review { flex: 1; text-align: center; }
  .pf-btn-deny, .pf-btn-review, .pf-btn-details {
    min-height: 44px; padding: 12px 14px; font-size: 13px;
  }
  /* Native checkboxes render near 13px, well under any usable target. */
  .pf-approval-row > input[type="checkbox"],
  .pf-approval-group-header input[type="checkbox"],
  .pf-select-all input[type="checkbox"] { width: 20px; height: 20px; }
  .pf-approval-row > input[type="checkbox"] { margin-top: 6px; }
  .pf-select-all, .pf-approval-group-header label { min-height: 44px; }
  /* Select-all, the count, and the two batch actions stop sharing one
     line. A grid rather than a wrapping flex row because the two buttons
     have to end up side by side and equal, which wrapping alone decides by
     whatever happens to fit -- and grid keeps source and visual order
     identical, so nothing here reorders focus. */
  .pf-approvals-toolbar {
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: center;
  }
  .pf-select-all, .pf-selected-count { grid-column: 1 / -1; }
  .pf-selected-count:empty { display: none; }
  .pf-btn-approve-selected { grid-column: 1; }
  .pf-btn-deny-selected { grid-column: 2; }
  .pf-btn-approve-selected, .pf-btn-deny-selected { min-height: 44px; }
}
"""

# Runtime dispatch: sessionStorage's pending toast (left by the card page's
# own shim right before it navigates back here -- see
# web/routes_approvals.py's _bridge_shim), the empty-state/row re-render on
# every "approvals" SSE event, row-level Deny (a direct POST to the decide
# endpoint, no navigation to the card at all -- §2.2's own point: denying
# needs no context), and the approval binder's own selection/deny-selected/
# inline-details behavior (see module docstring).
_JS = """
(function () {
  // Selection state -- module-scoped closure, survives every render() call
  // (see module docstring: __pfRenderApprovals replaces #pf-approvals-list
  // wholesale on every SSE tick, so this Set is the only thing that does).
  var pfSelected = new Set();
  var pfLastRows = [];

  function relAge(iso) {
    if (!iso) return '';
    var then = new Date(iso).getTime();
    if (isNaN(then)) return '';
    var s = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (s < 60) return 'just now';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ago';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function groupLabel(connector, operationKey, count) {
    var connLabel = connector ? connector.charAt(0).toUpperCase() + connector.slice(1) : 'Unknown';
    var verb = operationKey && operationKey.indexOf('.') !== -1
      ? operationKey.slice(operationKey.indexOf('.') + 1) : (operationKey || '');
    var verbLabel = verb ? verb.replace(/_/g, ' ') : '';
    var parts = [connLabel];
    if (verbLabel) { parts.push(verbLabel); }
    return parts.join(' \\u00b7 ') + ' \\u00b7 ' + count;
  }

  // Mirrors approval_list_html._group_rows -- see that function's own
  // docstring. Every batchable row sharing (connector, operation_key)
  // collapses into one group, positioned at that key's first appearance;
  // every non-batchable row (a confirm/choice dialog, or a PII-forced card)
  // stands alone with no checkbox.
  function groupRows(rows) {
    var groups = [];
    var index = {};
    rows.forEach(function (row) {
      if (!row.batchable) {
        groups.push({key: null, label: '', rows: [row]});
        return;
      }
      var key = (row.connector || '') + '\\u0000' + (row.operation_key || '');
      var group = index[key];
      if (!group) {
        group = {key: key, connector: row.connector || '', operationKey: row.operation_key || '', rows: []};
        index[key] = group;
        groups.push(group);
      }
      group.rows.push(row);
    });
    return groups;
  }

  function detailsHtml(id) {
    return '<div class="pf-approval-details" id="pf-details-' + esc(id) + '" hidden></div>';
  }

  // The connectors this document carries an icon rule for -- names only,
  // no image data: the rule itself is already in the page's own <style>
  // (see approval_list_html._icon_css), so a live-re-rendered row draws
  // the real mark by naming the same class the first paint did.
  var pfIconConnectors = %(icon_connectors)s;
  var pfIconAgents = %(icon_agents)s;
  var pfUnrecognised = %(unrecognised)s;
  var pfNotVerified = %(not_verified)s;

  // Mirrors approval_list_html._icon_html.
  function iconHtml(connector, initial) {
    if (pfIconConnectors.indexOf(connector) !== -1) {
      return '<div class="pf-approval-icon pf-approval-icon-img pf-approval-icon-' + connector + '"></div>';
    }
    return '<div class="pf-approval-icon pf-approval-icon-fallback">' + esc(initial) + '</div>';
  }

  // Mirrors approval_list_html._pill_html -- see that function.
  function pillHtml(gateKind) {
    if (gateKind === 'review') { return '<span class="pf-approval-pill pf-approval-pill-read">Read</span>'; }
    if (gateKind === 'popup') { return '<span class="pf-approval-pill pf-approval-pill-write">Write</span>'; }
    return '';
  }

  // Mirrors approval_list_html._agent_html -- see that function. Only an
  // attested row draws a mark, and only one this page baked a rule for.
  function agentHtml(agent) {
    agent = agent || {};
    var tier = ['attested', 'claimed', 'unknown'].indexOf(agent.tier) !== -1 ? agent.tier : 'unknown';
    var headline = agent.headline || pfUnrecognised;
    var mark = '';
    if (tier === 'attested') {
      if (pfIconAgents.indexOf(agent.icon_id) !== -1) {
        mark = '<span class="pf-approval-agent-mark pf-approval-agent-mark-' + agent.icon_id +
          '" aria-hidden="true"></span>';
      }
    } else {
      mark = '<span class="pf-approval-agent-glyph" aria-hidden="true">?</span>';
    }
    var text = agent.claim ? headline + ' \u201c' + agent.claim + '\u201d' : headline;
    var unverified = tier === 'attested' ? ''
      : '<span class="pf-approval-agent-unverified">\u00b7 ' + esc(pfNotVerified) + '</span>';
    return '<span class="pf-approval-agent pf-approval-agent-' + tier + '" data-agent-tier="' + tier + '">' +
      mark + '<span class="pf-approval-agent-name">' + esc(text) + '</span>' + unverified + '</span>';
  }

  function rowHtml(row) {
    // Mirrors approval_list_html._row_html: the object is the headline
    // (summary), the tool that touches it is the meta line.
    var title = esc(row.summary || row.tool_name || (row.kind === 'card' ? 'Approval' : 'Confirmation'));
    var kicker = [row.tool_name || '',
      row.connector ? row.connector.charAt(0).toUpperCase() + row.connector.slice(1) : '',
      relAge(row.created_at)].filter(Boolean).join(' \\u00b7 ');
    var initial = (row.connector || '?').charAt(0).toUpperCase();
    var checkbox = row.batchable
      ? '<input type="checkbox" data-select="' + esc(row.id) + '" aria-label="Select this approval">' : '';
    var blockedNote = (!row.batchable && row.blocked_reason)
      ? '<div class="pf-approval-blocked-reason">' + esc(row.blocked_reason) + '</div>' : '';
    return '<div class="pf-approval-row' + (row.batchable ? '' : ' pf-approval-row-unbatchable') + '"' +
      ' data-approval-id="' + esc(row.id) + '" data-tool="' + esc(row.tool || '') + '"' +
      ' data-batchable="' + (row.batchable ? '1' : '0') + '">' +
      checkbox +
      iconHtml(row.connector || '', initial) +
      '<div class="pf-approval-main">' +
      '<div class="pf-approval-meta">' + pillHtml(row.gate_kind || '') +
      '<span class="pf-approval-kicker">' + esc(kicker) + '</span>' + agentHtml(row.agent) + '</div>' +
      '<div class="pf-approval-title">' + title + '</div>' + blockedNote + '</div>' +
      '<div class="pf-approval-actions">' +
      '<button type="button" class="pf-btn-details" data-details="' + esc(row.id) + '">Details</button>' +
      '<a class="pf-btn-review" href="/approvals/' + esc(row.id) + '">Review \\u2192</a>' +
      '<button type="button" class="pf-btn-deny" data-deny="' + esc(row.id) + '">Deny</button></div>' +
      detailsHtml(row.id) +
      '</div>';
  }

  function groupHtml(group) {
    if (!group.key) { return rowHtml(group.rows[0]); }
    var label = groupLabel(group.connector, group.operationKey, group.rows.length);
    var header = group.rows.length > 1
      ? '<div class="pf-approval-group-header"><label>' +
        '<input type="checkbox" data-group-select="' + esc(group.key) + '" aria-label="Select all in this group">' +
        '<span>' + esc(label) + '</span></label></div>'
      : '';
    return '<div class="pf-approval-group" data-group-key="' + esc(group.key) + '">' + header +
      group.rows.map(rowHtml).join('') + '</div>';
  }

  function selectableIds(rows) {
    return rows.filter(function (r) { return r.batchable; }).map(function (r) { return r.id; });
  }

  // Q1 of the binder plan's own "Open questions": name the selected set's
  // composition on the submit button itself -- "Approve 12 · 9 reads, 3
  // writes" -- so an unintended write can't hide inside a read-shaped
  // batch. gate_kind is "popup" (write) | "review" (read) | "" (a bare
  // confirm/choice dialog, never batchable, so never counted here).
  function selectedComposition(rows) {
    var reads = 0, writes = 0;
    rows.forEach(function (r) {
      if (!pfSelected.has(r.id)) return;
      if (r.gate_kind === 'popup') { writes++; } else if (r.gate_kind === 'review') { reads++; }
    });
    return {reads: reads, writes: writes};
  }

  function compositionLabel(composition) {
    var parts = [];
    if (composition.reads) { parts.push(composition.reads + (composition.reads === 1 ? ' read' : ' reads')); }
    if (composition.writes) { parts.push(composition.writes + (composition.writes === 1 ? ' write' : ' writes')); }
    return parts.join(', ');
  }

  // Mirrors approval_list_html._heading_html. The heading lives outside
  // #pf-approvals-list, so unlike the rows it is not replaced by render()'s
  // own innerHTML write -- without this it keeps whatever count the first
  // paint happened to have, indefinitely.
  function updateHeading(rows) {
    var el = document.getElementById('pf-approvals-heading');
    if (!el) return;
    el.textContent = '';
    if (!rows.length) return;
    var reads = 0, writes = 0;
    rows.forEach(function (r) {
      if (r.gate_kind === 'popup') { writes++; } else if (r.gate_kind === 'review') { reads++; }
    });
    var count = document.createElement('span');
    count.className = 'pf-approvals-count';
    count.textContent = rows.length + ' approval' + (rows.length === 1 ? '' : 's') + ' pending';
    el.appendChild(count);
    var parts = [];
    if (reads) { parts.push(reads + (reads === 1 ? ' read' : ' reads')); }
    if (writes) { parts.push(writes + (writes === 1 ? ' write' : ' writes')); }
    if (!parts.length) return;
    var composition = document.createElement('span');
    composition.className = 'pf-approvals-composition';
    composition.textContent = parts.join(' \\u00b7 ');
    el.appendChild(composition);
  }

  function updateToolbar(rows) {
    var toolbar = document.getElementById('pf-approvals-toolbar');
    if (!toolbar) return;
    // Mirrors #pf-approvals-heading's own always-emitted/kept-in-sync
    // pattern -- see build_list_html's own comment. Without this, a
    // toolbar created empty (rows.length === 0 at first paint) never
    // reappears once rows start arriving live.
    toolbar.hidden = rows.length === 0;
    var ids = selectableIds(rows);
    var selectedCount = ids.filter(function (id) { return pfSelected.has(id); }).length;
    var composition = selectedComposition(rows);
    var countEl = document.getElementById('pf-selected-count');
    if (countEl) {
      // Names the denominator explicitly ("10 of 11 selected") rather than
      // just the numerator -- a bare "10 selected" reads as "10 of 10" the
      // moment it's glanced at, which is indistinguishable from the
      // denominator having silently shrunk (issue #576, bug 2).
      countEl.textContent = selectedCount === 0 ? '' : selectedCount + ' of ' + ids.length + ' selected';
    }
    var approveBtn = document.getElementById('pf-approve-selected');
    if (approveBtn) {
      approveBtn.disabled = selectedCount === 0;
      approveBtn.textContent = selectedCount === 0
        ? 'Approve selected'
        : 'Approve ' + selectedCount + (compositionLabel(composition) ? ' \\u00b7 ' + compositionLabel(composition) : '');
    }
    var denyBtn = document.getElementById('pf-deny-selected');
    if (denyBtn) { denyBtn.disabled = selectedCount === 0; }
    var selectAll = document.getElementById('pf-select-all-cb');
    if (selectAll) {
      selectAll.checked = ids.length > 0 && selectedCount === ids.length;
      selectAll.indeterminate = selectedCount > 0 && selectedCount < ids.length;
      selectAll.disabled = ids.length === 0;
    }
    // Per-group select-all reflects only its own group's rows.
    document.querySelectorAll('[data-group-select]').forEach(function (cb) {
      var key = cb.getAttribute('data-group-select');
      var groupIds = rows.filter(function (r) {
        return r.batchable && (r.connector || '') + '\\u0000' + (r.operation_key || '') === key;
      }).map(function (r) { return r.id; });
      var groupSelected = groupIds.filter(function (id) { return pfSelected.has(id); }).length;
      cb.checked = groupIds.length > 0 && groupSelected === groupIds.length;
      cb.indeterminate = groupSelected > 0 && groupSelected < groupIds.length;
    });
  }

  function applySelectionToCheckboxes() {
    document.querySelectorAll('[data-select]').forEach(function (cb) {
      cb.checked = pfSelected.has(cb.getAttribute('data-select'));
    });
  }

  function render(rows) {
    pfLastRows = rows || [];
    // Reconcile: an id no longer present (decided, expired) can't stay
    // selected -- otherwise a stale id would sit in pfSelected forever,
    // silently inflating "N selected" with nothing real behind it.
    var liveIds = new Set(pfLastRows.map(function (r) { return r.id; }));
    pfSelected.forEach(function (id) { if (!liveIds.has(id)) { pfSelected.delete(id); } });

    var container = document.getElementById('pf-approvals-list');
    if (container) {
      if (!pfLastRows.length) {
        container.innerHTML = %(empty)s;
      } else {
        container.innerHTML = groupRows(pfLastRows).map(groupHtml).join('');
      }
      applySelectionToCheckboxes();
    }
    updateHeading(pfLastRows);
    updateToolbar(pfLastRows);
  }
  window.__pfRenderApprovals = render;

  function denyOne(id) {
    return fetch('/api/approvals/' + encodeURIComponent(id) + '/decide', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({result: 'deny', csrf: %(csrf)s}),
    });
  }

  function denyRow(id) {
    denyOne(id).then(function (r) {
      var row = document.querySelector('[data-approval-id="' + id + '"]');
      if (row) { row.remove(); }
      pfSelected.delete(id);
      updateToolbar(pfLastRows);
      // §4.4's "offer it after the first successful decision" isn't only
      // the card page's own decision (whose own return-to-list flow
      // triggers this same prompt via the DOMContentLoaded handler below)
      // -- row-level Deny is a first-class decision path (§2.2's whole
      // point: denying needs no card) and never goes through that flow at
      // all, so without this, a workflow that only ever denies from the
      // list would never trigger the notification permission pre-prompt.
      if (r.ok && window.__pfNotifPrompt) { window.__pfNotifPrompt(); }
    });
  }

  function denySelected() {
    var ids = Array.from(pfSelected);
    if (!ids.length) return;
    var denyBtn = document.getElementById('pf-deny-selected');
    if (denyBtn) { denyBtn.disabled = true; }
    Promise.all(ids.map(function (id) {
      return denyOne(id).then(function (r) {
        if (r.ok || r.status === 409) {
          var row = document.querySelector('[data-approval-id="' + id + '"]');
          if (row) { row.remove(); }
          pfSelected.delete(id);
        }
        return r;
      });
    })).then(function (results) {
      updateToolbar(pfLastRows);
      if (results.some(function (r) { return r.ok; }) && window.__pfNotifPrompt) { window.__pfNotifPrompt(); }
    });
  }

  // The approval binder's own batch-approve path (Phase 3 of the binder
  // plan): a single POST to the batch decide endpoint (Phase 2), gated on
  // one WebAuthn assertion bound to the exact submitted set when step-up
  // applies. Mirrors web/routes_settings.py's own pfSettingsPost 428/403
  // handling almost exactly -- this page stays open across the ceremony
  // (it is not a one-shot card), so a failure surfaces via window.alert
  // rather than replacing the whole document.
  function submitBatch(items, batchId, assertion) {
    var body = {items: items, csrf: %(csrf)s};
    if (batchId) { body.batch_id = batchId; }
    if (assertion) { body.webauthn_assertion = assertion; }
    return fetch('/api/approvals/batch/decide', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  }

  function runBatch(items, batchId, assertion) {
    return submitBatch(items, batchId, assertion).then(function (r) {
      if (r.status === 428) {
        return r.json().then(function (data) {
          if (data.webauthn_options && window.PublicKeyCredential) {
            return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function (newAssertion) {
              return runBatch(items, data.batch_id, newAssertion);
            }).catch(function (err) {
              window.alert('Approving needs your passkey, and the prompt failed: ' + err.message);
              return null;
            });
          }
          window.alert('Approving needs a passkey, and none is available in this browser.');
          return null;
        });
      }
      if (r.status === 401) {
        window.alert('That passkey check could not be verified — please try Approve again.');
        return null;
      }
      if (r.status === 403 || r.status === 400) {
        return r.json().then(function (data) {
          if (data.enroll_url) {
            window.alert('This install requires a passkey to approve. Set one up at ' + data.enroll_url + '.');
          } else if (data.message) {
            window.alert(data.message);
          } else {
            // A stale/expired challenge (e.g. the passkey prompt took too
            // long) or another genuine 400 -- no silent no-op either way.
            window.alert('Could not approve this batch — please reload and try again.');
          }
          return null;
        });
      }
      return r.ok ? r.json() : null;
    });
  }

  function applyBatchResults(data) {
    var results = (data && data.results) || [];
    var appliedAny = false;
    results.forEach(function (result) {
      if (result.outcome === 'applied' || result.outcome === 'already_decided') {
        if (result.outcome === 'applied') { appliedAny = true; }
        var row = document.querySelector('[data-approval-id="' + result.id + '"]');
        if (row) { row.remove(); }
        pfSelected.delete(result.id);
      }
    });
    updateToolbar(pfLastRows);
    if (appliedAny && window.__pfNotifPrompt) { window.__pfNotifPrompt(); }
  }

  function approveSelected() {
    var ids = Array.from(pfSelected);
    if (!ids.length) return;
    var items = ids.map(function (id) { return {id: id, result: 'accept'}; });
    var approveBtn = document.getElementById('pf-approve-selected');
    if (approveBtn) { approveBtn.disabled = true; }
    runBatch(items, null, null).then(function (data) {
      if (data) { applyBatchResults(data); }
    }).catch(function () {
      window.alert('Could not submit the batch — please reload and try again.');
    }).then(function () {
      updateToolbar(pfLastRows);
    });
  }

  function toggleSelect(id, checked) {
    if (checked) { pfSelected.add(id); } else { pfSelected.delete(id); }
    updateToolbar(pfLastRows);
  }

  function toggleGroup(key, checked) {
    pfLastRows.forEach(function (r) {
      if (r.batchable && (r.connector || '') + '\\u0000' + (r.operation_key || '') === key) {
        if (checked) { pfSelected.add(r.id); } else { pfSelected.delete(r.id); }
      }
    });
    applySelectionToCheckboxes();
    updateToolbar(pfLastRows);
  }

  function toggleSelectAll(checked) {
    selectableIds(pfLastRows).forEach(function (id) {
      if (checked) { pfSelected.add(id); } else { pfSelected.delete(id); }
    });
    applySelectionToCheckboxes();
    updateToolbar(pfLastRows);
  }

  var pfDetailsCache = {};

  function detailsRow(container, key, value) {
    var row = document.createElement('div');
    row.className = 'pf-approval-details-row';
    var k = document.createElement('span');
    k.className = 'pf-approval-details-key';
    k.textContent = key + ':';
    var v = document.createElement('span');
    v.textContent = value;
    row.appendChild(k);
    row.appendChild(v);
    container.appendChild(row);
  }

  // `toolId` is the raw MCP tool id. It used to be the row's own kicker,
  // where it displaced the connector and the age without telling anyone
  // what the request was about; it belongs here, with the rest of the
  // metadata someone opening a disclosure is asking for. It comes off the
  // row payload this page already has -- no extra /preview field.
  function renderDetails(container, preview, toolId) {
    container.textContent = '';
    var keys = Object.keys(preview || {});
    if (toolId) { detailsRow(container, 'Tool', toolId); }
    if (!keys.length) {
      if (!toolId) { container.textContent = 'No further details.'; }
      return;
    }
    keys.forEach(function (key) {
      detailsRow(container, key, preview[key]);
    });
  }

  // Read off the row element rather than pfLastRows, which is only
  // populated once __pfRenderApprovals has run at least once -- on first
  // paint the rows are server-rendered and that array is still empty.
  function toolIdFor(id) {
    var row = document.querySelector('[data-approval-id="' + id + '"]');
    return (row && row.getAttribute('data-tool')) || '';
  }

  function toggleDetails(id) {
    var container = document.getElementById('pf-details-' + id);
    if (!container) return;
    if (!container.hasAttribute('hidden')) {
      container.setAttribute('hidden', 'hidden');
      return;
    }
    if (pfDetailsCache[id]) {
      renderDetails(container, pfDetailsCache[id], toolIdFor(id));
      container.removeAttribute('hidden');
      return;
    }
    fetch('/api/approvals/' + encodeURIComponent(id) + '/preview', {credentials: 'same-origin'})
      .then(function (r) { return r.ok ? r.json() : {preview: {}}; })
      .then(function (data) {
        pfDetailsCache[id] = data.preview || {};
        renderDetails(container, pfDetailsCache[id], toolIdFor(id));
        container.removeAttribute('hidden');
      })
      .catch(function () {
        container.textContent = 'Could not load details.';
        container.removeAttribute('hidden');
      });
  }

  document.addEventListener('click', function (e) {
    var denyBtn = e.target.closest('[data-deny]');
    if (denyBtn) { denyRow(denyBtn.getAttribute('data-deny')); return; }
    var detailsBtn = e.target.closest('[data-details]');
    if (detailsBtn) { toggleDetails(detailsBtn.getAttribute('data-details')); return; }
    if (e.target.id === 'pf-deny-selected') { denySelected(); return; }
    if (e.target.id === 'pf-approve-selected') { approveSelected(); return; }
  });

  document.addEventListener('change', function (e) {
    var cb = e.target.closest('[data-select]');
    if (cb) { toggleSelect(cb.getAttribute('data-select'), cb.checked); return; }
    var groupCb = e.target.closest('[data-group-select]');
    if (groupCb) { toggleGroup(groupCb.getAttribute('data-group-select'), groupCb.checked); return; }
    if (e.target.id === 'pf-select-all-cb') { toggleSelectAll(e.target.checked); return; }
  });

  // The card page's own return-to-list flow (web/routes_approvals.py's
  // _bridge_shim) stashes a toast message here right before navigating
  // back -- shown once, then cleared, so a page refresh never re-shows it.
  // Deferred to DOMContentLoaded: #pf-shell-toast and window.__pfNotifPrompt
  // are both defined by web_shell.py's own markup/script, which come
  // *after* this <script> in document order (this module's body_html is
  // wrapped inside <main>, ahead of the shell's own footer) -- running
  // this inline, synchronously, would find neither yet.
  document.addEventListener('DOMContentLoaded', function () {
    try {
      var raw = sessionStorage.getItem('pf_toast');
      if (raw) {
        sessionStorage.removeItem('pf_toast');
        var toast = JSON.parse(raw);
        var el = document.getElementById('pf-shell-toast');
        if (el && toast && toast.msg) {
          el.textContent = toast.msg;
          el.classList.add('shown');
          setTimeout(function () { el.classList.remove('shown'); }, 4000);
        }
        // §4.4: offer the notification permission pre-prompt right after
        // the first successful decision, when the value is concrete --
        // never on page load. window.__pfNotifPrompt (web_shell.py) itself
        // no-ops past the first time (localStorage) and past a non-default
        // permission state.
        if (window.__pfNotifPrompt) { window.__pfNotifPrompt(); }
      }
    } catch (e) { /* sessionStorage unavailable -- toast just doesn't show */ }
  });

  // §3 point 4: focus moves to the next pending row's Review control, not
  // into a re-opened card -- never auto-advance into a decision.
  var firstReview = document.querySelector('.pf-btn-review');
  if (firstReview) { firstReview.focus({preventScroll: true}); }
})();
"""


def row_from_approval(card: Any) -> dict[str, Any]:
    """``PendingApproval`` -> the same summary shape
    ``PendingApproval.to_summary_dict()`` already produces (approvals.py) --
    used for the server-rendered first paint here rather than calling that
    method directly, only so this module stays the one place that decides
    what a row needs to render (kept in sync with to_summary_dict() by the
    field names below, not by importing it, since the two shapes need to
    stay decoupled: an SSE payload field this module doesn't use yet
    shouldn't have to be added here too).

    ``batchable``/``blocked_reason`` prefer calling ``card``'s own
    ``is_batchable()``/``blocked_reason()`` (a real ``PendingApproval``) but
    fall back to computing the same thing from ``kind``/
    ``pii_forces_confirmation`` directly -- this module's own tests build a
    plain duck-typed stand-in (not every field of the real dataclass), and
    that fallback is what keeps this function working against either."""
    kind = getattr(card, "kind", "card")
    pii_forced = bool(getattr(card, "pii_forces_confirmation", False))
    if hasattr(card, "is_batchable"):
        batchable = bool(card.is_batchable())
    else:
        batchable = kind == "card" and not pii_forced
    if hasattr(card, "blocked_reason"):
        blocked_reason = card.blocked_reason()
    else:
        blocked_reason = "" if batchable else "This request can't be decided from the list."
    return {
        "id": card.id,
        "kind": kind,
        "connector": card.connector,
        "tool": card.tool,
        "tool_name": card.tool_name,
        "gate_kind": getattr(card, "gate_kind", ""),
        "operation_key": getattr(card, "operation_key", None) or "",
        "summary": card.summary,
        "created_at": _iso(card.created_at),
        "batchable": batchable,
        "blocked_reason": blocked_reason,
        "agent": agent_label.label_for(getattr(card, "agent", UNKNOWN_AGENT)).to_dict(),
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _group_label(connector: str, operation_key: str, count: int) -> str:
    conn_label = connector.capitalize() if connector else "Unknown"
    verb = operation_key.split(".", 1)[1] if "." in operation_key else operation_key
    verb_label = verb.replace("_", " ") if verb else ""
    parts = [p for p in (conn_label, verb_label) if p]
    header = " · ".join(parts) if parts else "Approvals"
    return f"{header} · {count}"


def _group_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Buckets ``rows`` (``row_from_approval()``'s shape) into the binder's
    own display groups (Phase 1: "Groups by (connector, operation_key)"):
    every batchable row sharing the same ``(connector, operation_key)``
    collapses into one group -- positioned at that key's first
    appearance in ``rows`` -- with a per-group select-all rendered only once
    there's more than one row in it; every non-batchable row (a confirm/
    choice dialog, or a PII-forced card -- see
    approvals.PendingApproval.is_batchable's own docstring) stands alone,
    with no checkbox, carrying its own ``blocked_reason``. The JS mirror is
    ``groupRows`` in this module's own ``_JS`` string.
    """
    groups: list[dict[str, Any]] = []
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not row.get("batchable"):
            groups.append({"key": None, "rows": [row]})
            continue
        key = (row.get("connector") or "", row.get("operation_key") or "")
        group = index.get(key)
        if group is None:
            group = {"key": key, "rows": []}
            index[key] = group
            groups.append(group)
        group["rows"].append(row)
    return groups


def _details_html(row_id: str) -> str:
    return f'<div class="pf-approval-details" id="pf-details-{_html_escape(row_id)}" hidden></div>'


def _connector_icon_uri(connector: str) -> str:
    return approval_icons.icon_data_uri(approval_icons.connector_icon_path(connector))


# Interpolated into a CSS selector and a class attribute, so it is
# restricted to what a connector identifier can legitimately be rather
# than escaped -- a name that doesn't match simply gets the letter badge.
_SAFE_CONNECTOR_RE = re.compile(r"^[a-z0-9_-]+$")


def _icon_slug(connector: str) -> str:
    slug = (connector or "").lower()
    return slug if _SAFE_CONNECTOR_RE.match(slug) else ""


def _icon_html(connector: str, initial: str, *, has_icon: bool) -> str:
    """The row's connector mark, or a letter badge when no icon is bundled
    for that connector. The mark is drawn from a per-connector CSS class
    (see ``_icon_css``) rather than an inline ``src``, so the image data
    appears once per document instead of once per row -- and so the JS
    mirror (``iconHtml``) can render the identical element without being
    handed any image data at all."""
    if has_icon:
        slug = _icon_slug(connector)
        return f'<div class="pf-approval-icon pf-approval-icon-img pf-approval-icon-{slug}"></div>'
    return f'<div class="pf-approval-icon pf-approval-icon-fallback">{_html_escape(initial)}</div>'


def _icon_connectors() -> dict[str, str]:
    """``{connector: data URI}`` for every connector this build bundles an
    icon for -- ``approval_icons.all_connector_icons()``'s whole known set,
    not just whichever connectors happen to have a row on this page.

    The live re-render is driven by ``PendingApproval.to_summary_dict()``
    (web/state_stream.py), which carries no icon and shouldn't: these are
    ~15-135KB PNGs, and base64'ing them into every SSE tick would cost far
    more than the letter badge it would replace. Emitting one CSS rule per
    connector instead means the live re-render needs no image data at all
    -- it renders the same class name and the rule already in the document
    does the rest.

    This used to build the map from ``rows`` alone, so a connector with
    nothing pending at first paint had no rule, and a row that arrived for
    it later drew the letter badge until the next full page load (issue
    #576). Baking in the whole bundled set instead of just the rows present
    right now closes that gap entirely: the set is small and fixed (~10
    files today), so this is a bounded, one-time cost per page load, not a
    per-row or per-tick one -- the SSE tick itself still never carries any
    image data, which is the cost this design was actually protecting
    against."""
    uris: dict[str, str] = {}
    for connector, uri in approval_icons.all_connector_icons().items():
        slug = _icon_slug(connector)
        if slug and uri:
            uris[slug] = uri
    return uris


def _icon_css(icon_uris: dict[str, str]) -> str:
    if not icon_uris:
        return ""
    rules = "".join(
        f'.pf-approval-icon-{slug}{{background-image:url("{uri}")}}'
        for slug, uri in sorted(icon_uris.items())
    )
    return (
        ".pf-approval-icon-img{background-size:contain;"
        "background-repeat:no-repeat;background-position:center}" + rules
    )


_GATE_PILL = {"review": ("read", "Read"), "popup": ("write", "Write")}


def _pill_html(gate_kind: str) -> str:
    """The row's own Read/Write pill, from ``gate_kind`` -- already in
    ``row_from_approval``'s output and already driving the Approve-selected
    composition label, so this needs no new data. "" for a bare confirm/
    choice dialog, which has no direction. The JS mirror is ``pillHtml``."""
    variant = _GATE_PILL.get(gate_kind)
    if variant is None:
        return ""
    modifier, text = variant
    return f'<span class="pf-approval-pill pf-approval-pill-{modifier}">{text}</span>'


def _agent_html(agent: dict[str, Any] | None) -> str:
    """The row's "who is asking" label, from ``agent_label.AgentLabel.to_dict()``
    (``row_from_approval``'s and ``to_summary_dict()``'s ``agent`` field).
    A row with no ``agent`` at all renders as unknown -- never blank and
    never "Claude". The mark is drawn from a per-agent CSS class, like the
    connector icon, and only for the attested tier: a claimed row's
    ``icon_id`` is "" already, and a tier other than attested is refused
    here as well. The JS mirror is ``agentHtml``."""
    agent = agent or UNKNOWN_AGENT_LABEL.to_dict()
    tier = agent.get("tier") or TIER_UNKNOWN
    if tier not in _AGENT_TIERS:
        tier = TIER_UNKNOWN
    headline = agent.get("headline") or UNRECOGNISED_LABEL
    slug = _icon_slug(agent.get("icon_id") or "")
    if tier == TIER_ATTESTED and slug in _agent_icon_uris():
        mark = f'<span class="pf-approval-agent-mark pf-approval-agent-mark-{slug}" aria-hidden="true"></span>'
    elif tier == TIER_ATTESTED:
        mark = ""
    else:
        mark = '<span class="pf-approval-agent-glyph" aria-hidden="true">?</span>'
    claim = agent.get("claim") or ""
    text = f"{headline} “{claim}”" if claim else headline
    unverified = (
        "" if tier == TIER_ATTESTED
        else f'<span class="pf-approval-agent-unverified">· {NOT_VERIFIED.lower()}</span>'
    )
    return (
        f'<span class="pf-approval-agent pf-approval-agent-{tier}" data-agent-tier="{tier}">'
        f'{mark}<span class="pf-approval-agent-name">{_html_escape(text)}</span>{unverified}</span>'
    )


def _agent_icon_uris() -> dict[str, str]:
    """``{agent_id: data URI}`` for every bundled agent mark -- the
    ``_icon_connectors()`` counterpart, for the same reason: the live
    re-render names a class and never carries image data."""
    uris: dict[str, str] = {}
    for agent_id, uri in approval_icons.all_agent_icons().items():
        slug = _icon_slug(agent_id)
        if slug and uri:
            uris[slug] = uri
    return uris


def _agent_icon_css(icon_uris: dict[str, str]) -> str:
    return "".join(
        f'.pf-approval-agent-mark-{slug}{{background-image:url("{uri}")}}'
        for slug, uri in sorted(icon_uris.items())
    )


def _row_html(row: dict[str, Any]) -> str:
    label = "Confirmation" if row.get("kind") != "card" else "Approval"
    # The object is the headline; the tool that touches it is the meta
    # line. `summary` is the one field naming what the request is actually
    # about ("Read \"Q3 forecast — legal review\"" -- see gate.py's own
    # connector call sites), and it used to be a fallback title that a
    # normal row never reached, because tool_name is always populated. The
    # fallback chain stays for a confirm/choice dialog, which has no
    # summary at all.
    title = row.get("summary") or row.get("tool_name") or label
    connector = (row.get("connector") or "").capitalize()
    kicker = " · ".join(
        p for p in (row.get("tool_name") or "", connector, _relative_age(row.get("created_at", ""))) if p
    )
    rid = row["id"]
    batchable = bool(row.get("batchable"))
    icon_html = _icon_html(
        row.get("connector") or "", connector[:1],
        has_icon=bool(_connector_icon_uri(row.get("connector") or "")),
    )
    checkbox_html = (
        f'<input type="checkbox" data-select="{_html_escape(rid)}" aria-label="Select this approval">'
        if batchable else ""
    )
    blocked_reason = row.get("blocked_reason") or ""
    blocked_html = (
        f'<div class="pf-approval-blocked-reason">{_html_escape(blocked_reason)}</div>'
        if not batchable and blocked_reason else ""
    )
    row_class = "pf-approval-row" if batchable else "pf-approval-row pf-approval-row-unbatchable"
    return (
        f'<div class="{row_class}" data-approval-id="{_html_escape(rid)}" '
        f'data-tool="{_html_escape(row.get("tool") or "")}" '
        f'data-batchable="{"1" if batchable else "0"}">'
        f"{checkbox_html}"
        f"{icon_html}"
        '<div class="pf-approval-main">'
        '<div class="pf-approval-meta">'
        f'{_pill_html(row.get("gate_kind") or "")}'
        f'<span class="pf-approval-kicker">{_html_escape(kicker)}</span>'
        f'{_agent_html(row.get("agent"))}'
        "</div>"
        f'<div class="pf-approval-title">{_html_escape(title)}</div>'
        f"{blocked_html}"
        "</div>"
        '<div class="pf-approval-actions">'
        f'<button type="button" class="pf-btn-details" data-details="{_html_escape(rid)}">Details</button>'
        f'<a class="pf-btn-review" href="/approvals/{_html_escape(rid)}">Review →</a>'
        f'<button type="button" class="pf-btn-deny" data-deny="{_html_escape(rid)}">Deny</button>'
        "</div>"
        f"{_details_html(rid)}"
        "</div>"
    )


def _group_html(group: dict[str, Any]) -> str:
    rows = group["rows"]
    if group["key"] is None:
        return _row_html(rows[0])
    connector, operation_key = group["key"]
    header_html = ""
    if len(rows) > 1:
        label = _group_label(connector, operation_key, len(rows))
        group_key_attr = f"{connector}\x00{operation_key}"
        header_html = (
            '<div class="pf-approval-group-header"><label>'
            f'<input type="checkbox" data-group-select="{_html_escape(group_key_attr)}" '
            'aria-label="Select all in this group">'
            f"<span>{_html_escape(label)}</span></label></div>"
        )
    group_key_attr = f"{connector}\x00{operation_key}"
    body = "".join(_row_html(r) for r in rows)
    return f'<div class="pf-approval-group" data-group-key="{_html_escape(group_key_attr)}">{header_html}{body}</div>'


def _relative_age(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _composition_label(rows: list[dict[str, Any]]) -> str:
    """"3 reads · 1 write" for the page heading -- the same read/write split
    the Approve-selected button already names for a *selected* set, applied
    to the whole queue. The JS mirror is ``headingHtml``/``compositionLabel``
    in this module's own ``_JS`` string."""
    reads = sum(1 for r in rows if r.get("gate_kind") == "review")
    writes = sum(1 for r in rows if r.get("gate_kind") == "popup")
    parts = []
    if reads:
        parts.append(f"{reads} read{'s' if reads != 1 else ''}")
    if writes:
        parts.append(f"{writes} write{'s' if writes != 1 else ''}")
    return " · ".join(parts)


def _heading_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    count = f"{len(rows)} approval{'s' if len(rows) != 1 else ''} pending"
    composition = _composition_label(rows)
    return (
        f'<span class="pf-approvals-count">{_html_escape(count)}</span>'
        + (
            f'<span class="pf-approvals-composition">{_html_escape(composition)}</span>'
            if composition else ""
        )
    )


def _toolbar_html(*, any_batchable: bool, hidden: bool) -> str:
    select_all_disabled = "" if any_batchable else " disabled"
    hidden_attr = " hidden" if hidden else ""
    return (
        f'<div class="pf-approvals-toolbar" id="pf-approvals-toolbar"{hidden_attr}>'
        '<label class="pf-select-all">'
        f'<input type="checkbox" id="pf-select-all-cb" aria-label="Select all batchable approvals"'
        f"{select_all_disabled}>"
        "<span>Select all</span></label>"
        '<span class="pf-selected-count" id="pf-selected-count"></span>'
        '<button type="button" class="pf-btn-approve-selected" id="pf-approve-selected" disabled>'
        "Approve selected</button>"
        '<button type="button" class="pf-btn-deny-selected" id="pf-deny-selected" disabled>Deny selected</button>'
        "</div>"
    )


def build_list_html(
    rows: list[dict[str, Any]], *, csrf: str, nonce: str | None = None, any_authed: bool = True,
) -> str:
    """The ``/approvals`` page body (dropped into web_shell.wrap's
    ``<main>``) -- ``rows`` is a list of row_from_approval()'s shape,
    newest first (same order approvals.PendingApprovalRegistry.
    list_pending() already returns).

    ``nonce``: the caller's current per-response CSP nonce (web/server.py's
    ``_SecurityHeadersMiddleware``, via ``request.state.csp_nonce``) --
    unlike approval_window_html.py's card documents, this fragment is
    rendered fresh on every ``GET /approvals``, so it takes the request's
    own nonce rather than minting one itself. Must be the same value
    web_shell.wrap() is given for the rest of this same document, since
    only one Content-Security-Policy header covers both. Defaults to a
    fresh one when omitted (every caller outside this module's own tests
    always passes the real per-request value explicitly).

    ``any_authed``: whether this install has at least one authenticated
    connector, which selects between the two empty states (see
    ``_empty_state_html``). Defaults to True -- the steady-state copy --
    so a caller that cannot determine it never shows a first-run message
    to somebody who is already set up."""
    nonce = nonce or secrets.token_urlsafe(18)
    empty_state = _empty_state_html(any_authed=any_authed)
    body = "".join(_group_html(g) for g in _group_rows(rows)) if rows else empty_state
    # Always emitted, like #pf-approvals-heading below -- render() below
    # keeps it in sync (including its "hidden" state) on every SSE tick, and
    # an element that only exists when the first paint had rows is an
    # element the live re-render can't create later (issue #576, bug 1).
    toolbar = _toolbar_html(any_batchable=any(r.get("batchable") for r in rows), hidden=not rows)
    icon_uris = _icon_connectors()
    agent_icon_uris = _agent_icon_uris()
    js = _JS % {
        # Already branched: whether a connector is authenticated is fixed
        # for this document's lifetime, so render() needs the resolved
        # string rather than the flag and a second copy of the branch.
        "empty": json.dumps(empty_state),
        "csrf": json.dumps(csrf),
        # Names only -- the image data is in the <style> block below, once.
        "icon_connectors": json.dumps(sorted(icon_uris)),
        "icon_agents": json.dumps(sorted(agent_icon_uris)),
        "unrecognised": json.dumps(UNRECOGNISED_LABEL),
        "not_verified": json.dumps(NOT_VERIFIED.lower()),
    }
    return (
        f'<style nonce="{nonce}">{_CSS}{_icon_css(icon_uris)}{_agent_icon_css(agent_icon_uris)}</style>'
        '<div class="pf-approvals-page">'
        # Always emitted, even with nothing pending: render() below updates
        # it on every SSE tick, and an element that only exists when the
        # first paint had rows is an element the live re-render can't reach.
        f'<div class="pf-approvals-heading" id="pf-approvals-heading">{_heading_html(rows)}</div>'
        + toolbar
        + f'<div id="pf-approvals-list">{body}</div>'
        "</div>"
        f'<script nonce="{nonce}">{js}</script>'
    )
