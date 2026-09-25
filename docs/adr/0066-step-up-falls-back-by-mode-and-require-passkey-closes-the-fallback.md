# ADR 0066: Step-up falls back by mode, and `require_passkey` closes the fallback

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-08-28 in
`docs/https-connector-refactor-plan.md`'s "step-up options" section, deleted in `96cd5af4`: read it
with `git show 96cd5af4^:docs/https-connector-refactor-plan.md`). Org mode's passkey-or-IdP step-up
landed in `40f45600`, merged as [#218](https://github.com/privacyfence/privacyfence/pull/218).
`step_up.require_passkey` closed the IdP fallback in `dd2c7312`
([#446](https://github.com/privacyfence/privacyfence/pull/446), fixing
[#406](https://github.com/privacyfence/privacyfence/issues/406)). Local mode's decide-time step-up
landed in `87361dce` ([#469](https://github.com/privacyfence/privacyfence/pull/469)), and its
`require_passkey` hard fail in `2fc760aa` ([#471](https://github.com/privacyfence/privacyfence/pull/471)),
both under [#426](https://github.com/privacyfence/privacyfence/issues/426). The org-mode batch
refusal below is implemented, fixing https://github.com/privacyfence/privacyfence/issues/741.

## Context

Step-up asks for a fresh WebAuthn assertion before an approving decision is released. A principal
can reach that point with no passkey enrolled, and the product has to decide what happens then. The
two modes differ in what else they have. Org mode has an IdP it can send the human back to. Local
mode has none.

## Decision

For a single approving decision that `step_up.scope` covers:

- **Org mode** (`_org_step_up_response` in `web/routes_approvals.py`). With `require_passkey` off,
  the `428` always carries `idp_stepup_url`, alongside `webauthn_options` when a passkey is
  enrolled. That URL starts an IdP re-authentication (`web/routes_org_stepup.py`) with
  `prompt=login` and `max_age=0`, plus `acr_values` when `IdpConfig.step_up_acr_values` is set.
  The callback releases the decision only if the re-authenticated principal is the one the step-up
  was started for and, where ACR values are configured, the returned `acr` is one of them. Org mode
  therefore never releases a decision without one of the two ceremonies.
- **Local mode** (`_local_step_up_response`). With `require_passkey` off and nothing enrolled, the
  factory returns `None` and `decide()` releases the decision without step-up. Deadlocking every
  approval behind a ceremony nobody can complete is worse than this gap, and the gap is closed by
  `require_passkey`.
- **`require_passkey` closes the fallback in both modes.** With it on and nothing enrolled, both
  factories return a `403` with `passkey_enrollment_required` and `enroll_url: "/security"`. In
  org mode `idp_stepup_url` is no longer offered, and `stepup_idp_start` refuses with a `403` even
  when a client calls it directly.
- Operators set this in the org bundle with `scripts/build_org_bundle.py`'s WebAuthn step-up
  options (`--step-up-require-passkey`, `--idp-step-up-acr-value`).

Only single decisions get the IdP fallback. Batch step-up (ADR 0065) and sensitive settings
actions (ADR 0034) are passkey-only in both modes. With `require_passkey` off and nothing enrolled,
a batch follows its mode's single-decision rule: local mode applies it without step-up, and org
mode refuses it with a `400` and applies nothing, so org mode has no path that releases an
approving decision without a ceremony. Sensitive settings actions are gated only when
`require_passkey` is on.

## Alternatives considered

- **IdP re-authentication as the only step-up.** Rejected: `prompt=login`/`max_age=0` defends
  against a stolen session cookie and nothing more. On the owner's own unlocked device, a password
  manager's autofill completes it. `acr_values` step-up is only as strong as whatever the IdP
  enforces for that value. A user-verified passkey is what stands against a handed-over or
  compromised session, so it is the primary path and IdP re-authentication is the fallback.
- **Hard-fail in local mode even with `require_passkey` off.** Rejected: a local install with
  step-up enabled but nothing enrolled would block every approving decision. That lockout is what
  `require_passkey` opts into on purpose. Packaged installs default it on (ADR 0003's *Out of
  scope* amendment), and the companion walks the human through a first enrollment.

## Consequences

- An org that enabled step-up to defend against a phished or compromised IdP session must also set
  `require_passkey`. Without it, the IdP session the attacker holds is also the fallback.
- A local install with `require_passkey` off can be evaded by never enrolling a passkey. That is a
  documented property of that configuration, not a bug.
- With `require_passkey` on and nothing enrolled, approving is blocked until a passkey is enrolled
  at `/security` (ADR 0069).

## Verification

- `tests/unit/web/test_routes_approvals.py`: `TestStepUpEvadableWithNoPasskeyEnrolled`,
  `TestRequirePasskeyHardFail`, and `TestStepUpWebAuthnFlow`'s check that local mode never offers
  an IdP URL.
- `tests/unit/web/test_routes_org_approvals.py`: `TestStepUpWebAuthnFlow` (the IdP link with and
  without a credential), `TestStepUpRequirePasskey`, `TestIdpStepUp`
  (`test_callback_requires_configured_acr_values_when_set`), and `TestBatchStepUp`'s
  `test_require_passkey_off_with_nothing_enrolled_refuses_the_batch_with_nothing_applied`. Local
  mode's batch fall-through is `tests/unit/web/test_routes_approvals.py`'s `TestBatchStepUp`
  `test_require_passkey_off_with_nothing_enrolled_lets_it_through`.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) decision 6: a session alone must
  not release an approval.
- [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md): org mode's own OAuth
  server, separate from the IdP it re-authenticates against.
- [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md): the settings half.
- [ADR 0065](0065-approving-from-the-list-is-a-batch-bound-to-one-step-up-assertion.md): batch
  step-up, which has no IdP fallback.
