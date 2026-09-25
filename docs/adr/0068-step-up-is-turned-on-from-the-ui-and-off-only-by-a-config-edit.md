# ADR 0068: Step-up is turned on from the UI and off only by a config edit

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-17). The no-UI-path posture
and the "treat this install as compromised" notice came first, in `4189454f`, merged as
[#474](https://github.com/privacyfence/privacyfence/pull/474) under
[#426](https://github.com/privacyfence/privacyfence/issues/426). The enable-only UI path was added
in `32dfaaeb` ([#484](https://github.com/privacyfence/privacyfence/pull/484)).

## Context

Local mode's step-up settings live in `config/settings.yaml`. On a privilege-separated install that
file is not writable by the user's account, so with no UI path, turning step-up on meant `sudo`, a
text editor and a daemon restart.

Daemon startup compares the loaded `step_up.enabled`/`require_passkey` pair with what the last
startup saw (`webauthn_stepup.observe_step_up_requirement`). A transition into "not required"
latches `step_up_disabled_notice`, a banner on every page that says "If you didn't do this, treat
this install as compromised", until a later startup restores the requirement. That warning is only
worth something if no ordinary session could have produced the disable.

## Decision

- **On is a UI action.** `SettingsController.enable_step_up` sets `step_up.enabled` and
  `step_up.require_passkey` to true together and writes them to `config/settings.yaml`. It then
  calls `LiveStepUpConfig.update()`, so every consumer sees the change on its next request with no
  restart, and calls `observe_step_up_requirement` itself so the enable is audited immediately. It
  refuses, leaving the config untouched, unless a passkey is already enrolled or when
  `StepUpConfig.from_local_config` would reject the result (an unseparated install, ADR 0003).
- **Off is not.** No settings action turns either flag off, and `enable_step_up` is the only caller
  of `LiveStepUpConfig.update()`. Disabling takes a hand edit of `config/settings.yaml` and a
  restart, and that restart is when `observe_step_up_requirement` sees the transition and latches
  the notice.
- `enable_step_up` is in `_SENSITIVE_ACTIONS`, so once step-up is already required, calling it
  again needs a fresh assertion like any other sensitive action.
- The Settings page's Security card has no "turn off" control (`settings_window_html.py`'s
  `renderGeneral`). It shows either the enable button, a hint to add a passkey first, or nothing
  once step-up is on.
- Org mode has no equivalent. Its `StepUpConfig` comes from the org bundle, which an administrator
  writes.

## Alternatives considered

- **A toggle that goes both ways.** Rejected: a disable reachable from a session is reachable by
  any process holding one, including the agent step-up defends against. The disabled notice would
  then fire for ordinary use too, and the human could no longer treat it as evidence of tampering.
- **No UI path at all.** Rejected: the product's main security control would then need a shell and
  root to turn on. The enable direction is safe to expose because it only ever makes the install
  stricter.

## Consequences

- The disabled notice means something specific: whoever turned step-up off could edit the root-owned
  config and restart the daemon, which a session alone cannot do.
- Turning step-up off stays deliberately awkward. That is the intended cost.
- `LiveStepUpConfig.update()` itself accepts any `StepUpConfig`. The one-way property is held by
  its only caller, not by the holder; a new caller would need the same restriction.

## Verification

- `tests/unit/test_settings_controller.py`: `TestEnableStepUp` (refused with nothing enrolled,
  both flags set together).
- `tests/unit/web/test_routes_settings.py`: `TestEnableStepUpAction` and
  `TestEnableStepUpRefusesOnAnUnseparatedInstall`.
- `tests/unit/test_settings_window_html.py`: `TestStepUpCard`.
- `tests/unit/test_webauthn_stepup.py`: `observe_step_up_requirement` transitions and
  `TestStepUpDisabledNotice`.

## Related

- [ADR 0003](0003-separated-installs-only.md): why `config/settings.yaml` is out of the agent's
  reach.
- [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md): the sensitive-action
  gate `enable_step_up` sits in.
- [ADR 0069](0069-require-passkey-with-nothing-enrolled-starts-and-releases-nothing.md): the state
  `enable_step_up` refuses to create.
