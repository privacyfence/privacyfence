# ADR 0065: Approving from the list is a batch bound to one step-up assertion

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-08-28 to 2026-09-18). The
list's "no Allow on the row" rule dates from the list's first design, `2d81ee78`, merged as
[#187](https://github.com/privacyfence/privacyfence/pull/187). Batch approval came from the approval
binder work: `4dcc1b4b` ([#505](https://github.com/privacyfence/privacyfence/pull/505)),
`1f18811c` ([#510](https://github.com/privacyfence/privacyfence/pull/510)) and `27d82d8b`
([#512](https://github.com/privacyfence/privacyfence/pull/512)). The server-minted `batch_id`
followed in `94960763` ([#516](https://github.com/privacyfence/privacyfence/pull/516)). The design
note that recorded it was deleted in `f5b57380`: read it with
`git show f5b57380^:docs/approval-list-ui-ux.md`.

## Context

`/approvals` lists every pending approval as a one-line summary. The card behind each row is where
the human sees what a request actually does. Approving from a one-line summary is the habituation
failure the card exists to prevent. Denying from one is harmless, because a deny releases nothing.

A sequential agent can still leave many approvals pending at once. Reviewing each one on its own
card, with its own passkey ceremony, was the stall the approval binder set out to fix.

## Decision

- **No per-row Allow.** A row carries Details, Review and Deny, in that order, and never an Allow
  button (`approval_list_html.py`'s module docstring). Review opens the real card. Approving a
  single approval always goes through its card.
- **Row Deny needs no card and no step-up.** It posts `result: "deny"` to the per-id decide
  endpoint. `_STEP_UP_RESULTS` in `web/routes_approvals.py` holds only the approving results
  (`accept`, `accept_all`). Deny-selected runs client-side over that same endpoint.
- **The only way to approve from the list is Approve selected**, which covers only batchable rows
  (`PendingApproval.is_batchable()`). It posts the selected set to `batch_decide`
  (`POST /api/approvals/batch/decide`) with `result: "accept"` on every item. When any known,
  batchable, approving item needs step-up under `step_up.scope`, the batch is gated on **one**
  WebAuthn assertion bound to the exact submitted set: `webauthn_stepup.batch_decision_fingerprint`
  covers the principal and every `(id, result)` pair, deny items included. The button label names
  the set's composition ("Approve 12 · 9 reads, 3 writes"). On a local install, an approving batch
  also passes the same human-session check as a single approving decision.
- **`batch_id` is minted by the server.** The 428 challenge carries a `batch_id` the client echoes
  back. `batch_decide` keeps a client-supplied `batch_id` for the audit record only when
  `approval_step_up.guard_batch_decision` returns `batch_id_verified=True`, which means a
  resubmitted assertion verified against a live challenge this server issued under
  `batch:<batch_id>`. On every other path the endpoint records a fresh `uuid4`.
- **Batch mode.** `step_up.batch` defaults to `single_assertion` (`DEFAULT_STEP_UP_BATCH_MODE`).
  `per_item` makes the batch endpoint refuse a batch that needs step-up: a `400` with
  `batch_step_up_per_item`, and nothing in the request is applied. It never falls back to one
  assertion per item. A deny-only batch is unaffected either way.
- **Batch step-up never offers an IdP fallback, in either mode.** `batch_step_up_response`
  returns a 428 with passkey options, or a 403 naming `/security` when `require_passkey` is on and
  nothing is enrolled. With `require_passkey` off and nothing enrolled it returns `None`, and the
  batch is applied without step-up. That is local mode's single-decision fall-through, and here it
  applies in org mode too.

## Alternatives considered

- **Allow on each row.** Rejected: approving from a summary line is exactly the habituation the
  card exists to prevent. A human who wants one approval approves it on its card.
- **One passkey ceremony per item inside a batch.** Rejected: it restores the stall the binder
  exists to remove. The same assertion bound to the whole set gives the same freshness and user
  verification, and binding every item means the set cannot be widened, narrowed or flipped on
  resubmission. An install that wants no single prompt to cover several decisions sets `per_item`.
  That refuses the batch outright, so no downgrade happens silently.
- **An `<iframe>` of the real card for inline Details.** Rejected: card documents ship
  `frame-ancestors 'none'`, and the list's own `frame-src` admits only `data:`. Embedding the card
  would mean weakening the CSP for looks. Details instead renders the metadata-only preview from
  `GET /api/approvals/{id}/preview` with `textContent`.

## Consequences

- One assertion establishes that a verified human approved this exact set. It does not establish
  that the human read each item; the binder trades one ceremony per decision for one per batch.
  What a selected row discloses is the preview metadata, and Review stays available per item.
- Kinds that `is_batchable()` excludes (confirm and choice dialogs, PII-forced items) can still
  only be approved on their own card.
- An audit entry's `batch_id` groups decisions only when one verified ceremony actually covered
  them. A client cannot make unrelated decisions look like one human action.
- In org mode with `require_passkey` off, a single decision is never released without step-up
  (ADR 0066), but a batch with nothing enrolled is. `require_passkey` closes both.

## Verification

- `tests/unit/test_approval_list_html.py`: `test_never_renders_an_allow_button`.
- `tests/unit/web/test_routes_approvals.py`: `TestBatchDecide` (including
  `test_a_client_supplied_batch_id_is_never_recorded_verbatim`) and `TestBatchStepUp` (set
  binding, flipped results, replay, `require_passkey`, and both `per_item` cases).
- `tests/unit/web/test_routes_org_approvals.py`: `TestBatchStepUp`
  (`test_no_assertion_offers_a_428_with_no_idp_url`).

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) decision 6: a session alone must
  not release an approval.
- [ADR 0066](0066-step-up-falls-back-by-mode-and-require-passkey-closes-the-fallback.md): the
  single-decision fallback by mode.
- [ADR 0067](0067-the-default-step-up-scope-is-writes-and-pii-reads.md): which items need
  step-up.
