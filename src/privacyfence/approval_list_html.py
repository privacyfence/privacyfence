"""Pure-function HTML for the ``/approvals`` list page
(docs/approval-list-ui-ux.md §2, the P1-compatible slice its own §6 says
can land ahead of P3's full design -- the row shape, the empty state, and
the central asymmetry of §2.2: **Deny is on the row; Allow is never on the
row.** Denying without reading the card cannot leak anything; approving
from a one-line summary is exactly the habituation failure the card exists
to prevent, so there is no "Allow" button here at all -- only "Review",
which opens the real card.

``build_list_html(rows)`` is the first paint (web/routes_approvals.py, given
``approvals.PendingApproval`` objects, each with a real connector icon --
see ``row_from_approval`` below); ``window.__pfRenderApprovals(state)`` is
the live re-render web_shell.py's SSE dispatch calls with
web/state_stream.py's own "approvals" event payload
(``PendingApproval.to_summary_dict()``, which carries no icon -- building
one needs approval_icons.py, a filesystem read this module deliberately
doesn't do on every SSE tick). A live-updated row therefore renders with a
plain connector-initial badge instead of the real icon; a decided row
leaves the list within one poll interval regardless (web/state_stream.py's
``_APPROVALS_POLL_SECONDS``), so the visual gap is real but short-lived --
a documented simplification of this phase's own P1-compatible scope, not
an oversight.

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

Approving still opens the card through Phase 2 -- docs/approval-list-ui-ux.md's
own claim ("no Allow on the list") stays literally true up to there.

**Phase 3 of the binder plan** is what finally breaks that claim: an
**Approve selected** button posts the same set to the batch decide
endpoint (Phase 2) with ``result: "accept"`` on every item, gated
server-side on one WebAuthn assertion bound to the exact submitted set
(webauthn_stepup.batch_decision_fingerprint) whenever step-up applies.
``runBatch`` mirrors web/routes_settings.py's own ``pfSettingsPost``
428/403 handling almost exactly: a ``428`` carries fresh
``webauthn_options`` (and this page's own ``batch_id`` to echo back) to
complete with ``window.pfWebauthnGet`` (injected by web/routes_approvals.py's/
web/routes_org_approvals.py's list route, same as the settings page) and
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
import secrets
from datetime import datetime, timezone
from html import escape as _html_escape
from typing import Any

from . import approval_icons

_EMPTY_STATE = (
    '<div class="pf-approvals-empty">'
    '<div class="pf-approvals-empty-title">Nothing is waiting.</div>'
    '<div class="pf-approvals-empty-sub">PrivacyFence is watching.</div>'
    "</div>"
)

_CSS = """
.pf-approvals-page { max-width: 720px; margin: 0 auto; padding: 24px 20px 60px; width: 100%; }
.pf-approvals-heading { font-size: 13px; color: var(--color-neutral-600); margin-bottom: 14px; }
.pf-approvals-empty {
  text-align: center; padding: 80px 20px; color: var(--color-neutral-600);
}
.pf-approvals-empty-title { font-size: 16px; font-weight: 600; color: var(--color-text); margin-bottom: 4px; }
.pf-approvals-empty-sub { font-size: 13px; }
.pf-approvals-toolbar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 14px; margin-bottom: 12px; background: var(--color-surface); border-radius: var(--radius-lg);
}
.pf-select-all { display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
.pf-selected-count { font-size: 12.5px; color: var(--color-neutral-600); flex: 1; min-width: 0; }
.pf-btn-deny-selected {
  font-size: 12.5px; font-weight: 600; padding: 7px 12px; border-radius: var(--radius-md);
  border: 1px solid var(--color-divider); background: transparent; color: var(--color-danger); cursor: pointer;
}
.pf-btn-deny-selected:disabled { opacity: 0.5; cursor: default; }
.pf-btn-approve-selected {
  font-size: 12.5px; font-weight: 600; padding: 7px 12px; border-radius: var(--radius-md);
  border: 1px solid var(--color-accent); background: var(--color-accent); color: #fff; cursor: pointer;
}
.pf-btn-approve-selected:disabled { opacity: 0.5; cursor: default; }
.pf-approval-group { margin-bottom: 14px; }
.pf-approval-group-header {
  display: flex; align-items: center; gap: 8px; padding: 6px 4px; font-size: 12.5px;
  font-weight: 600; color: var(--color-neutral-600);
}
.pf-approval-group-header label { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.pf-approval-row {
  display: flex; align-items: center; gap: 12px; padding: 14px 16px;
  background: var(--color-surface); border-radius: var(--radius-lg); margin-bottom: 10px; flex-wrap: wrap;
}
.pf-approval-icon {
  width: 28px; height: 28px; border-radius: var(--radius-md); flex-shrink: 0; object-fit: contain;
  background: var(--color-neutral-200);
}
.pf-approval-icon-fallback {
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; color: var(--color-neutral-600);
}
.pf-approval-main { flex: 1; min-width: 0; }
.pf-approval-title {
  font-size: 14px; font-weight: 600; color: var(--color-text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.pf-approval-kicker { font-size: 12px; color: var(--color-neutral-600); margin-top: 2px; }
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

  function rowHtml(row) {
    var title = esc(row.tool_name || row.summary || (row.kind === 'card' ? 'Approval' : 'Confirmation'));
    var kicker = [row.connector ? row.connector.charAt(0).toUpperCase() + row.connector.slice(1) : '',
      row.tool || '', relAge(row.created_at)].filter(Boolean).join(' \\u00b7 ');
    var initial = (row.connector || '?').charAt(0).toUpperCase();
    var checkbox = row.batchable
      ? '<input type="checkbox" data-select="' + esc(row.id) + '" aria-label="Select this approval">' : '';
    var blockedNote = (!row.batchable && row.blocked_reason)
      ? '<div class="pf-approval-blocked-reason">' + esc(row.blocked_reason) + '</div>' : '';
    return '<div class="pf-approval-row' + (row.batchable ? '' : ' pf-approval-row-unbatchable') + '"' +
      ' data-approval-id="' + esc(row.id) + '" data-batchable="' + (row.batchable ? '1' : '0') + '">' +
      checkbox +
      '<div class="pf-approval-icon pf-approval-icon-fallback">' + esc(initial) + '</div>' +
      '<div class="pf-approval-main"><div class="pf-approval-title">' + title + '</div>' +
      '<div class="pf-approval-kicker">' + esc(kicker) + '</div>' + blockedNote + '</div>' +
      '<div class="pf-approval-actions">' +
      '<button type="button" class="pf-btn-details" data-details="' + esc(row.id) + '">Details</button>' +
      '<button type="button" class="pf-btn-deny" data-deny="' + esc(row.id) + '">Deny</button>' +
      '<a class="pf-btn-review" href="/approvals/' + esc(row.id) + '">Review \\u2192</a></div>' +
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

  function updateToolbar(rows) {
    var toolbar = document.getElementById('pf-approvals-toolbar');
    if (!toolbar) return;
    var ids = selectableIds(rows);
    var selectedCount = ids.filter(function (id) { return pfSelected.has(id); }).length;
    var composition = selectedComposition(rows);
    var countEl = document.getElementById('pf-selected-count');
    if (countEl) {
      countEl.textContent = selectedCount === 0 ? '' : selectedCount + ' selected';
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

  function renderDetails(container, preview) {
    container.textContent = '';
    var keys = Object.keys(preview || {});
    if (!keys.length) {
      container.textContent = 'No further details.';
      return;
    }
    keys.forEach(function (key) {
      var row = document.createElement('div');
      row.className = 'pf-approval-details-row';
      var k = document.createElement('span');
      k.className = 'pf-approval-details-key';
      k.textContent = key + ':';
      var v = document.createElement('span');
      v.textContent = preview[key];
      row.appendChild(k);
      row.appendChild(v);
      container.appendChild(row);
    });
  }

  function toggleDetails(id) {
    var container = document.getElementById('pf-details-' + id);
    if (!container) return;
    if (!container.hasAttribute('hidden')) {
      container.setAttribute('hidden', 'hidden');
      return;
    }
    if (pfDetailsCache[id]) {
      renderDetails(container, pfDetailsCache[id]);
      container.removeAttribute('hidden');
      return;
    }
    fetch('/api/approvals/' + encodeURIComponent(id) + '/preview', {credentials: 'same-origin'})
      .then(function (r) { return r.ok ? r.json() : {preview: {}}; })
      .then(function (data) {
        pfDetailsCache[id] = data.preview || {};
        renderDetails(container, pfDetailsCache[id]);
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


def _row_html(row: dict[str, Any]) -> str:
    label = "Confirmation" if row.get("kind") != "card" else "Approval"
    title = row.get("tool_name") or row.get("summary") or label
    connector = (row.get("connector") or "").capitalize()
    kicker = " · ".join(p for p in (connector, row.get("tool") or "", _relative_age(row.get("created_at", ""))) if p)
    rid = row["id"]
    batchable = bool(row.get("batchable"))
    icon_uri = approval_icons.icon_data_uri(approval_icons.connector_icon_path(row.get("connector", "")))
    icon_html = (
        f'<img class="pf-approval-icon" src="{_html_escape(icon_uri)}" alt="">' if icon_uri
        else f'<div class="pf-approval-icon pf-approval-icon-fallback">{_html_escape(connector[:1])}</div>'
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
        f'data-batchable="{"1" if batchable else "0"}">'
        f"{checkbox_html}"
        f"{icon_html}"
        '<div class="pf-approval-main">'
        f'<div class="pf-approval-title">{_html_escape(title)}</div>'
        f'<div class="pf-approval-kicker">{_html_escape(kicker)}</div>'
        f"{blocked_html}"
        "</div>"
        '<div class="pf-approval-actions">'
        f'<button type="button" class="pf-btn-details" data-details="{_html_escape(rid)}">Details</button>'
        f'<button type="button" class="pf-btn-deny" data-deny="{_html_escape(rid)}">Deny</button>'
        f'<a class="pf-btn-review" href="/approvals/{_html_escape(rid)}">Review →</a>'
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


def _toolbar_html(*, any_batchable: bool) -> str:
    select_all_disabled = "" if any_batchable else " disabled"
    return (
        '<div class="pf-approvals-toolbar" id="pf-approvals-toolbar">'
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


def build_list_html(rows: list[dict[str, Any]], *, csrf: str, nonce: str | None = None) -> str:
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
    always passes the real per-request value explicitly)."""
    nonce = nonce or secrets.token_urlsafe(18)
    body = "".join(_group_html(g) for g in _group_rows(rows)) if rows else _EMPTY_STATE
    heading = (
        f"{len(rows)} approval{'s' if len(rows) != 1 else ''} pending" if rows else ""
    )
    toolbar = _toolbar_html(any_batchable=any(r.get("batchable") for r in rows)) if rows else ""
    js = _JS % {"empty": json.dumps(_EMPTY_STATE), "csrf": json.dumps(csrf)}
    return (
        f'<style nonce="{nonce}">{_CSS}</style>'
        '<div class="pf-approvals-page">'
        + (f'<div class="pf-approvals-heading">{_html_escape(heading)}</div>' if heading else "")
        + toolbar
        + f'<div id="pf-approvals-list">{body}</div>'
        "</div>"
        f'<script nonce="{nonce}">{js}</script>'
    )
