# ADR 0069: With `require_passkey` on and nothing enrolled, the daemon starts and releases nothing step-up covers

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-17 in `2fc760aa`, merged as
[#471](https://github.com/privacyfence/privacyfence/pull/471) under
[#426](https://github.com/privacyfence/privacyfence/issues/426)).

## Context

`step_up.require_passkey` means an approving decision needs a passkey assertion, with no weaker
fallback (ADR 0066). A local install can have it on with nothing enrolled. On a packaged install
this is the normal first state, because the flag defaults on there (ADR 0003's *Out of scope*
amendment). It can also happen after a recovery code removed every credential.

In that state no approving decision can satisfy step-up. The daemon could treat this as a
configuration error and refuse to boot. But the only fix is enrolling a passkey at `/security`,
which the daemon itself serves.

## Decision

The daemon starts and serves in this state. It does not raise a `ConfigurationError`.

- Every decision that step-up covers is refused with a `403` naming `/security`
  (`passkey_enrollment_required`): an approving decision within `step_up.scope`, a sensitive
  confirm, an approving batch that contains such an item, and every sensitive settings action.
  Denies are unaffected.
- `StepUpConfig.local_enrollment_banner` returns a non-dismissable banner, which
  `web_shell.wrap()` renders on every `/approvals` and `/settings` page. It is re-derived on every
  request and disappears as soon as a passkey is enrolled.
- `daemon_main.py` logs a warning at startup naming `/security`, for someone reading only the log.
- On a packaged install the companion opens `/security` at each start until something is enrolled
  (ADR 0003's amendment), so the state is meant to last minutes.

## Alternatives considered

- **Refuse to boot.** Rejected: it removes the only path to `/security`, which is the page that
  fixes the problem. The human would be left editing a root-owned config file to turn the
  requirement off, which is the weaker posture, and a disable that latches ADR 0068's
  compromise notice.
- **Start and release approvals unguarded until something is enrolled.** Rejected: that is local
  mode's behaviour with `require_passkey` off (ADR 0066). Turning the flag on is exactly the
  request not to do that.

## Consequences

- The daemon is closed for releases and open for repair: `/security` and denies keep working, and
  nothing step-up covers is released.
- A decision outside `step_up.scope` is still released by a session. Under the default scope
  (ADR 0067) that is an unflagged read. The banner's wording ("approving decisions and sensitive
  settings changes are blocked") does not make that distinction.
- `enable_step_up` refuses to create this state from the UI (ADR 0068). It arises from a default
  or a hand edit, not from a click.

## Verification

- `src/privacyfence/step_up_config.py`: `StepUpConfig.local_enrollment_banner`.
- `src/privacyfence/daemon_main.py`: the startup warning after `server.start()`.
- `tests/unit/test_step_up_config.py`: `TestLocalEnrollmentBanner`.
- `tests/unit/web/test_routes_approvals.py`: `TestRequirePasskeyHardFail`,
  `TestRequirePasskeyBanner`, and `TestBatchStepUp`'s
  `test_require_passkey_with_nothing_enrolled_hard_fails_the_whole_batch`.

## Related

- [ADR 0003](0003-separated-installs-only.md): the packaged default and the companion's
  first-enrollment prompt.
- [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md): the settings actions
  that are also refused.
- [ADR 0066](0066-step-up-falls-back-by-mode-and-require-passkey-closes-the-fallback.md): what
  `require_passkey` closes.
