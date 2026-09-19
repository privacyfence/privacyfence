# Approval UI reference

PrivacyFence uses the embedded web UI for approvals. The implementation is shared across supported desktop platforms and org mode, with org-mode routes adding principal-aware authorization.

## Approval list

`GET /approvals` shows all currently pending approvals available to the authenticated user.

Each row contains:

- the operation summary as its title — what the request is *about*, not which tool makes it;
- a read/write pill, from the approval's own `gate_kind`;
- connector and tool identity, and relative age, on a meta line above the title;
- **Details**;
- **Review**;
- **Deny**.

The raw MCP tool id is not on the row. It appears in the **Details** disclosure alongside the rest
of the metadata-only preview. A row whose approval carries no summary — a bare confirm/choice
dialog — falls back to the tool name for its title, and a row with no direction (the same case)
renders no pill.

The page heading names the queue's own composition ("4 approvals pending · 3 reads · 1 write"), the
same read/write split the **Approve selected** button names for a selected set.

The action cluster is ordered Details, Review, Deny — the destructive action last, not adjacent to
the safe one. Denying resolves an approval outright and no undo path exists anywhere in the flow, so
Deny sits at the far end of the cluster. That ordering is source order in both the server-rendered
and the live-re-rendered row, not a CSS `order` override, so focus order and visual order stay the
same at every width.

When there are no pending requests the page shows an empty state, and which one depends on whether
this install has anything to govern:

- **At least one connector authenticated** — "Nothing is waiting. / PrivacyFence is watching."
- **Nothing authenticated** — "Nothing is governed yet.", explaining that PrivacyFence sits between
  Claude and the user's real accounts and holds nothing back until a connector exists, with a link
  to `/settings/connectors`. The steady-state copy is misleading here: nothing is waiting because
  nothing *can* wait, and the reassurance claims a protection that isn't running. The wording
  matches the settings page's own welcome banner, which is the only other place this state is
  explained.

The distinction is re-evaluated per request, so authenticating a connector takes effect on the next
page load. A caller that cannot determine it (no settings controller mounted) gets the steady-state
copy — never tell someone who is already set up that they are not.

## The approval binder

The rule that actually holds, and has held since the row-level Allow question was first raised, is
**no approval without disclosure** — not "no Allow action on the list". The list groups pending,
**batchable** rows by `(connector, operation_key)`, with a per-group and a page-level select-all,
and two selection-scoped actions:

- **Deny selected** clears a whole group of unwanted requests in one action, client-side over the
  existing per-id decide endpoint. Denying releases no protected content and performs no protected
  write, so it needs no passkey ceremony and no new server endpoint.
- **Approve selected** posts the same set to `POST /api/approvals/batch/decide` with every item's
  result set to `accept`. Server-side this is gated on one WebAuthn passkey assertion bound to the
  exact submitted set (`webauthn_stepup.batch_decision_fingerprint`) whenever step-up applies to
  anything selected — see [`security-and-compliance.md`](security-and-compliance.md#the-approval-binders-single-assertion)
  for what that assertion does and does not establish. The submit button names the selected set's
  composition ("Approve 12 · 9 reads, 3 writes") so an unintended write can't hide inside a
  read-shaped batch. It is styled as an outline, not a filled button: **Review** is the one filled
  control on the page and it is the one that opens disclosure, so the least-informed action is
  deliberately not also the loudest.

**Disclosure, not the full card, is what a selected row shows.** Each batchable row gets an inline
"Details" disclosure, fetched from a read-only `GET /api/approvals/{id}/preview` fragment — the
same metadata-only `preview` dict `gate.py` stamps onto every approval at registration
(`docs/coding-and-testing-guidelines.md` §1.5 bounds what a preview may ever contain). It is
rendered with `textContent`, never an `<iframe>` onto the real card document: card documents ship
`frame-ancestors 'none'` and this page's own `frame-src` admits `data:` only, so embedding the card
would mean weakening the CSP for cosmetics. **Approving a selected batch still never shows more
than that metadata disclosure** — the binder trades one ceremony per decision for one ceremony per
batch, not attention to any decision's full context for none. A human who wants the full card
before deciding one item still has **Review**, unchanged.

**Not every row is batchable, and never by silent default.** `PendingApproval.is_batchable()` and
`blocked_reason()` classify every approval kind explicitly — by membership in an allowed/excluded
pair, never by complement — with a coverage test that fails the moment a new kind isn't classified
either way, so nothing lands in the binder by falling through an `else`. Two kinds are excluded, each
for its own reason:

- a `confirm`/`choice` dialog — a mid-flight second step with a different result vocabulary than
  `accept`/`deny`;
- anything the PII scanner has already forced a second confirmation on (`pii_forces_confirmation`)
  — that confirmation only materializes *after* the card is answered, so batching the first step
  would spray a fresh queue of confirm dialogs into the list, defeating the point of the PII gate.

Excluded rows still appear in the list — with no checkbox, and `blocked_reason()` naming why — so
their existence isn't hidden, only their inclusion in a batch.

**Selection survives live updates.** It lives in the page's own JS state (a `Set` keyed by
approval id) and is reconciled after every SSE re-render rather than being wiped by one, since
`__pfRenderApprovals` replaces the list's markup wholesale on every tick.

## Live updates

The shared web shell consumes the approval state stream and re-renders the list when pending approvals change. Multiple approvals can be pending at the same time and are independently decidable.

The browser tab title and accessibility announcement can reflect the pending count. The web shell also exposes connection state so the user can see when the UI is connected/reconnecting.

**Local mode only.** Org mode's app mounts no `GET /api/state/stream`, so its pages render neither
the live indicator nor the stream script, and its tier-0/1 notifications are off with them. The
indicator says whether the queue in front of a reviewer is current; on a mode with no stream behind
it, showing one would either claim a liveness that does not exist or sit permanently on a connection
error. An org-mode list is correct as of load and does not claim otherwise.

## The shared shell

Both modes render through `web_shell.wrap`: one header, one nav, one palette, one favicon. What
differs is passed in, not forked:

- **nav items** — local mode has Approvals and Settings; org mode has Approvals, Connections,
  Passkeys and Settings. `/connect` genuinely has no local-mode equivalent (that mode's Connectors
  section of `/settings` is it), and org mode's `/settings` is its own much smaller surface.
  `/security` is a different case: it *is* mounted in local mode, it is simply not in that mode's
  nav — it is reached from the require-a-passkey banner, from the companion's own first-run
  enrollment prompt, or directly.
- **principal label** — org mode names the signed-in principal in the header. Its entire
  authorization model is per-principal and the page otherwise never says whose queue is on screen.
  Local mode has exactly one principal and renders nothing.
- **live updates** — see above.

## Approval card

**Review** opens `/approvals/{id}`. The full card is generated by `approval_window_html.py` and delivered by `web/routes_approvals.py`.

The card contains the operation-specific information documented in [`approval-window-content-reference.md`](approval-window-content-reference.md). The user can allow/deny the operation, and eligible flows can offer an always-allow rule scoped to the operation/resource the user reviewed.

A stale or already-decided approval is not resurrected; the page reports that the approval is no longer pending and links back to the list.

## Return-to-list behavior

After a successful decision, the browser returns to `/approvals` with a one-time toast such as the recorded/denied/already-decided status. Browser history is not used to reopen the completed approval as a fresh decision screen.

Focus returns to the list rather than automatically opening another pending request.

## Notifications

Notifications are optional and controlled by web notification settings.

- The pending count/title update needs no browser notification permission.
- Browser notifications are emitted through the registered service worker when enabled and permitted.
- Notification detail is limited according to the configured `minimal`, `standard`, or `detailed` level.
- Permission is offered after a meaningful user interaction/decision, not immediately on initial page load.

At `detailed` level the notification can include the approval summary; operators/users should treat that as potentially sensitive content appearing in the OS/browser notification surface.

## Responsive behavior

The approval list and cards are browser pages and must remain usable at desktop, tablet, and phone widths. Primary actions must remain visible without horizontal page scrolling. Subjective layout/contrast review is covered by [`release-testing.md`](release-testing.md); objective browser behavior belongs in Playwright tests.

Every document served here declares `<meta name="viewport" content="width=device-width, initial-scale=1">`. Without it a phone lays the document out in its default ~980px viewport and scales the result down to fit, which makes body text unreadable and leaves every phone-width `@media` rule in the document permanently unmatched. A test that only narrows a desktop browser's window cannot detect this, because a desktop context lays out at whatever width it is given; the Playwright coverage for it therefore runs in a `is_mobile=True` context and asserts on `window.innerWidth`.

Below 560px the list row stacks: identity on top, a full-width action strip underneath. Row controls and selection checkboxes are a minimum 44px target at those widths — above them the row stays a single line.

## Implementation anchors

- `src/privacyfence/approvals.py`
- `src/privacyfence/approval_list_html.py`
- `src/privacyfence/approval_window_html.py`
- `src/privacyfence/dialog_window_html.py`
- `src/privacyfence/web_approval_ui.py`
- `src/privacyfence/web/routes_approvals.py`
- `src/privacyfence/web/routes_org_approvals.py`
- `src/privacyfence/web/step_up_decide.py`
- `src/privacyfence/webauthn_stepup.py`
- `src/privacyfence/step_up_config.py`
- `src/privacyfence/web/state_stream.py`
- `src/privacyfence/web_shell.py`
- `src/privacyfence/resources/sw.js`
