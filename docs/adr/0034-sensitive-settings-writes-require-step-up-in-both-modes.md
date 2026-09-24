# ADR 0034: Sensitive settings writes require WebAuthn step-up in both local and org mode

## Status

Accepted — 2026-09-24. Implemented (PSC-4a).

## Context

#426 Phase 3 gated local mode's settings dispatcher (`web/routes_settings.py`) on WebAuthn step-up:
`_SENSITIVE_ACTIONS` — `add_policy_rule`, `remove_policy_rule`, `set_default_policy`,
`set_category_policy`, `toggle_calendar_free_busy`, `toggle_pii_detection`, `toggle_pii_category`,
`enable_connector`, `enable_step_up` — are the actions that change *what gets gated*, not merely
how the app looks or behaves. Its own module docstring states the reasoning directly: an agent that
cannot forge an approval decision does not need to, if it can just add an always-allow rule instead,
and `POST /api/settings/{action}` was that open door until step-up closed it. The approval surface
had the matching control already: `web/routes_org_approvals.py` took a `step_up` parameter and ran
the full ceremony on `decide()`.

Org mode's own bespoke settings routes (`web/routes_org_settings.py`, pre-PSC-4a) had none of this.
`build_routes()` took no `StepUpConfig` at all, and none of the four sensitive actions with an
org-mode route (`add_policy_rule`, `remove_policy_rule`, `toggle_pii_detection`/`toggle_pii_category`
via `/api/settings/privacy/pii`, `set_default_policy`/`set_category_policy` via
`/api/settings/privacy/policy`) demanded a fresh WebAuthn assertion before running. Org mode already
had every other precondition step-up needs — `StepUpConfig`, enrolled passkeys
(`web/routes_security.py` is mounted in both modes), and the ceremony itself
(`web/approval_step_up.py`, PSC-2a) — it was simply never wired into the settings write path. This
was flagged, and left open, by both PSC-2a and PSC-4b's respective phase briefs before this phase
picked it up; [Issue #579](https://github.com/privacyfence/privacyfence/issues/579) records it as
the strongest concrete argument for merging the settings route layer at all: the duplicated surface
did not get a second review, it got no step-up.

This is a trust-boundary gap, not a cosmetic inconsistency: it means the same security property —
"a fresh passkey assertion before a decision that changes future gating" — held for one mode and
not the other, on the exact same class of action, with no mode-specific reason for the asymmetry.

## Decision

**A sensitive settings action requires a fresh WebAuthn platform-authenticator assertion under
`step_up.require_passkey`, in local mode and org mode alike, with no weaker fallback for either.**
Org mode's four bespoke write routes are gated through `_apply_step_up_gate`/`_needs_step_up` — the
same two-round-trip protocol local mode's dispatcher already used (`428` carrying fresh options, or
`403` naming `/security` if nothing is enrolled, then a second POST carrying the completed
assertion), driven off the same `_SENSITIVE_ACTIONS` classification. `_record_settings_audit`, org
mode's own audit-logging call already present on its bespoke routes, is preserved unchanged by the
gating.

This lands ahead of, and independently from, the broader route-layer merge
([ADR 0033](0033-one-route-layer-per-surface-with-an-auth-adapter-per-mode.md)): PSC-4a closes the
gap inside org mode's own still-separate module, so the fix does not wait on the larger
consolidation to land. PSC-4b's later merge of the two dispatchers, and PSC-5's shared renderer
(which closes the matching WebAuthn *ceremony UI* gap — org mode's settings pages returning raw
JSON instead of the passkey prompt local mode already showed — as a consequence of sharing the
dispatch path; see [ADR 0032](0032-org-settings-share-locals-generic-action-dispatcher.md)), both
build on this decision rather than re-deciding it.

## Alternatives considered

- **Leave org mode's settings surface ungated until the route-layer merge lands.** Rejected: the
  gap is live and exploitable independent of whether or when the modules are merged, and the merge
  itself was still being scoped when this was found. A trust-boundary fix does not wait on an
  unrelated refactor's timeline.
- **Gate only the two actions org mode's admin-only Privacy Filter editor exposes
  (`set_default_policy`/`set_category_policy`), leaving `add_policy_rule`/`remove_policy_rule`
  ungated.** Rejected: `add_policy_rule`/`remove_policy_rule` are exactly the "add an always-allow
  rule instead of forging an approval" attack `_SENSITIVE_ACTIONS` was built to close in local mode;
  leaving them out in org mode would reopen the same door PSC-4a exists to shut.

## Consequences

- An org admin (or a compromised org session) can no longer add an always-allow rule, change the
  default/category policy, or flip PII detection without a fresh passkey assertion — the same
  guarantee local mode's settings dispatcher already gave, now held uniformly by mode.
- Every future action added to `_SENSITIVE_ACTIONS` is gated in both modes by construction once it
  has an org-mode route (`org_settings_scope.ACTION_SCOPES` membership), rather than needing a
  second, easy-to-forget gating decision per mode.
- Org mode's settings writes gained a step-up round trip they didn't previously need to handle;
  PSC-4b/PSC-5 (see [ADR 0032](0032-org-settings-share-locals-generic-action-dispatcher.md)) is
  what gives that round trip a passkey-prompt UI instead of a raw JSON `428`/`403`.

## Verification

- `web/routes_settings.py`'s `_SENSITIVE_ACTIONS`, `_needs_step_up()`, `_apply_step_up_gate()`, and
  `build_org_routes()`'s use of all three.
- `tests/unit/web/test_routes_org_settings.py` — an org-mode `add_policy_rule` (and the other three
  gated actions) under `step_up.require_passkey` gets a `428` and no written rule until a valid
  assertion is supplied.

## Related

- [Issue #579](https://github.com/privacyfence/privacyfence/issues/579) — "The settings half"
  section, which first named this gap and is why the issue's scope widened from approvals-only to
  include settings.
- [ADR 0033](0033-one-route-layer-per-surface-with-an-auth-adapter-per-mode.md) — the broader
  route-layer merge this decision precedes and does not depend on.
- [ADR 0032](0032-org-settings-share-locals-generic-action-dispatcher.md) — PSC-5's own scope
  decision, which closes the matching step-up *ceremony UI* gap as a consequence of sharing the
  dispatch path.
- PSC-4a (#635) — the PR that implemented this decision.
