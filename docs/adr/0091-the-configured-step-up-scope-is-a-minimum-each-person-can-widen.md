# ADR 0091: The configured step-up scope is a minimum each person can widen

## Status

Accepted — 2026-09-28, decided by the maintainer. Builds on
[ADR 0067](0067-the-default-step-up-scope-is-writes-and-pii-reads.md), whose default and ladder are
unchanged; this ADR changes what the configured value *means*.

## Context

`step_up.scope` decides which approving decisions need a passkey: `writes`, `writes_and_pii_reads`
or `writes_and_reads` (`STEP_UP_SCOPES` in `src/privacyfence/step_up_config.py`). In organization
mode it comes from the org bundle (`org_config.json`), which an administrator writes; in local mode
from `config/settings.yaml`. Either way it was one value for everyone, changeable only by editing
that file and restarting.

An organization sets the floor it needs. Some people want more than that for their own approvals,
for example a passkey on every read, without asking an administrator to impose it on everyone.

## Decision

- **The configured `step_up.scope` is the minimum.** Nothing reachable from a session can make an
  install ask for a passkey on less than it.
- **Each principal can widen it for their own approvals** from **Settings > General > Security >
  Passkey required for**, in both modes, admin or not. The action is `set_step_up_scope`. Rungs
  narrower than the configured scope are drawn locked with no action attached.
- **The effective scope is the wider of the two** (`step_up_config.effective_scope`). The single
  decision path and the batch path in `web/approval_step_up.py` both use it, and `/security`
  describes it.
- **The choice is stored per principal under `paths.authority_dir()`**
  (`step_up_preference.json`), next to the passkey credential store, not in `settings.yaml` or the
  org bundle. Under privilege separation (ADR 0003) the agent can no more narrow it than it can
  edit the credentials. A value at or below the configured scope is stored as no choice, so raising
  the configured scope later is never held back by an old personal value, and an unreadable or
  unknown value is ignored rather than trusted.
- **`set_step_up_scope` is a sensitive settings action** (ADR 0034): once a passkey is required for
  settings changes, changing it needs one, so narrowing back towards the minimum costs the passkey
  it would stop asking for. Every change is audited like any other settings change.
- Organization mode shows **General** to every principal so the control is reachable; the
  install-wide PII card on that page stays admin-only.

## Alternatives considered

- **Let the Settings page write `step_up.scope` itself.** Rejected: in organization mode the value
  belongs to the administrator and applies to everyone; in local mode it would let a session
  narrow the scope, which ADR 0068 keeps out of the UI for the same reason it keeps "off" out.
- **Only allow widening from the UI, with no way back short of a file edit.** Rejected: the
  widening is a personal preference, not the organization's policy. Gating the way back on a
  passkey keeps it out of an agent's reach without making a person's own choice permanent.
- **A personal override for `enabled`/`require_passkey` too.** Not done here: in organization mode
  step-up needs the administrator's relying-party setup, and local mode already has a Turn on
  button (ADR 0068). The control only appears where step-up is enabled.

## Consequences

- A person can ask for a passkey on every read they approve while the organization keeps
  `writes_and_pii_reads` for everyone else.
- Where step-up is off (`step_up.enabled: false`) the control is not shown and a stored choice has
  no effect until step-up is enabled.

## Verification

- `tests/unit/test_step_up_config.py`: `TestPersonalScopeWidensNeverNarrows`.
- `tests/unit/web/test_approval_step_up.py`: `TestPersonalScope`.
- `tests/unit/web/test_routes_settings.py`: `TestSetStepUpScope`.
- `tests/unit/web/test_routes_org_settings.py`: `TestOrgStepUpScope`.
- `tests/unit/test_settings_window_html.py`: `TestStepUpCard`.

## Related

- [ADR 0003](0003-separated-installs-only.md): why `authority_dir()` is out of the agent's reach.
- [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md): the sensitive-action
  gate.
- [ADR 0067](0067-the-default-step-up-scope-is-writes-and-pii-reads.md): the ladder and its default.
- [ADR 0068](0068-step-up-is-turned-on-from-the-ui-and-off-only-by-a-config-edit.md): the other
  one-way step-up control.
