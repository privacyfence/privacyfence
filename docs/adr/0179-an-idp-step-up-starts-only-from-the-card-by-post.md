# ADR 0179: An IdP step-up starts only from the card, by POST

## Status

Accepted — 2026-10-14. Implemented.

## Context

When organization mode has no passkey for a decision, ADR 0066 lets the person confirm it by
signing in again at the identity provider. The start of that sign-in was a `GET
/api/approvals/<id>/stepup/idp?result=...&choice=...` that checked only the session, and the 428
response carried the link in `idp_stepup_url`. A link is something an AI client can put in its own
output: a signed-in member who followed it started a fresh IdP sign-in whose callback released the
approval with a result and choice the link chose (SEC-B4).

## Decision

- **The start is a POST.** `stepup_idp_start` accepts only `POST` (a GET is 405). Its checks, in
  order: no session → 401 `signed_out`; step-up off → 404; foreign `Origin` → 403; non-JSON body →
  400; wrong CSRF (the same double-submit as other org POSTs) → 403; `require_passkey` → 403
  (the fallback stays closed); unknown `result` → 400. The body is `{"result", "choice", "csrf"}`
  and the answer is `{"redirect": <IdP URL>}`, not a 302.
- **The URL carries nothing.** `idp_stepup_url` in the 428 body is
  `/api/approvals/<id>/stepup/idp` with no query string.
- **The card's own button makes the call.** The org bridge shim writes a
  `Verify by signing in again` button and one `pfIdpStepUp(url, payload)` function posts the JSON
  and navigates to the returned `redirect`; there is no inline handler because the CSP forbids it.

## Alternatives considered

- **Land the IdP callback back on the card for a final click.** Rejected: one more click on every
  step-up, and the POST already removes the agent-supplied link.

## Consequences

- Nothing outside the approval card's own page can start a step-up; a link in a message does
  nothing.
- Anything that followed the old link, such as a bookmarked or scripted start, now gets 405.

## Verification

- `web/routes_org_stepup.py` (`stepup_idp_start`), `web/routes_approvals.py`
  (`_org_step_up_response`, `_org_bridge_shim`).
- `tests/unit/web/test_routes_org_approvals.py` (`TestIdpStepUp`: `get_without_csrf`,
  `cross_site_navigation`, `start_without_csrf`, `step_up_is_off`, `after_ttl`, the shim source).

## Related

- ADR [0066](0066-step-up-falls-back-by-mode-and-require-passkey-closes-the-fallback.md)
