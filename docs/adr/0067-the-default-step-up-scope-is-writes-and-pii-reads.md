# ADR 0067: The default step-up scope is writes and PII-flagged reads

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-19 in `8ab65ef6`, merged as
[#556](https://github.com/privacyfence/privacyfence/pull/556)). The scope ladder dates from
`40f45600` ([#218](https://github.com/privacyfence/privacyfence/pull/218)), which defaulted to
`writes`. Its widest rung, `writes_and_reads`, was added in `d33a4199`
([#531](https://github.com/privacyfence/privacyfence/pull/531)).

## Context

`step_up.scope` decides which approving decisions need a fresh passkey assertion. It has three
values, narrowest first (`STEP_UP_SCOPES` in `src/privacyfence/step_up_config.py`):

- `writes`: every gated write (`gate_kind == "popup"`);
- `writes_and_pii_reads`: those, plus a gated read that `pii_detector.py` flagged
  (`PendingApproval.pii_detected`);
- `writes_and_reads`: those, plus every gated read, flagged or not.

The original default, `writes`, was chosen with a human at the keyboard in mind. ADR 0003's
adversary is different: an agent with code execution on the same machine. Against that agent,
`writes` leaves every read releasable by a session alone, including a read the PII detector has
already marked as carrying personal data. Exfiltration is what such an agent wants, and `writes`
never asks a human about it.

## Decision

`DEFAULT_STEP_UP_SCOPE` is `"writes_and_pii_reads"`, and `StepUpConfig.scope` defaults to it. The
same default applies in both modes: `from_org_config` and `from_local_config` both fall back to it
when `step_up.scope` is absent. An install that sets `scope:` explicitly, in either mode, keeps
exactly what it set. `webauthn_stepup.is_step_up_required` is where the scope is applied.

## Alternatives considered

- **Keep `writes`.** Rejected: it leaves PII-flagged reads releasable by a session alone, which is
  the exfiltration path an agent with code execution would use.
- **Default to `writes_and_reads`.** Rejected: it asks for a passkey on reads that nothing marks as
  sensitive. Frequent prompts for unremarkable requests teach people to approve without reading,
  which is the habituation step-up is meant to prevent. It stays available for an install that
  does not trust the PII detector to have flagged everything worth confirming.
- **A different default per mode.** Rejected: one key meaning different things by mode is the
  drift `step_up_config.py` exists to prevent.

## Consequences

- A fresh install asks for a passkey on a flagged read as well as on every write.
- An unflagged read is still released by a session alone under the default. An install that wants
  every read covered opts into `writes_and_reads`.
- Existing installs whose config already names a scope are not changed. `scripts/build_org_bundle.py`'s
  `--step-up-scope` help text states the same default.

## Verification

- `src/privacyfence/step_up_config.py`: `DEFAULT_STEP_UP_SCOPE`, `StepUpConfig.scope`, and both
  `from_*_config` readers.
- `tests/unit/test_step_up_config.py`: an org config and a local config with no `step_up.scope`
  both resolve to `DEFAULT_STEP_UP_SCOPE`.
- `tests/unit/test_webauthn_stepup.py`: `TestIsStepUpRequired`, which covers each scope.

## Related

- [ADR 0003](0003-separated-installs-only.md): the adversary this default is chosen against.
- [ADR 0066](0066-step-up-falls-back-by-mode-and-require-passkey-closes-the-fallback.md): what
  happens when a covered decision needs step-up.
