# ADR 0199: An organization bundle carries a signed serial, and an older one is refused

## Status

Accepted — 2026-10-17. Implemented.
Amends [ADR 0016](0016-org-config-bundle-hash-log-and-signing.md): signing proved who made a bundle, not that it was the newest.

## Context

A signed organization bundle is verified against a trust-on-first-use pinned key (ADR 0016). Nothing
ordered bundles, so anyone able to write the install directory could put back an older, genuinely
signed bundle, for example one from before a connector or a restriction was added, and it would verify.

## Decision

A signed bundle may carry a top-level `serial`, a positive JSON integer (a `bool` is not one),
covered by the signature like every other field. An install pins the highest serial it has accepted
in `org_config_serial.txt` beside `org_config_signing_pubkey.txt` (0600), and refuses a bundle with a
lower one. An equal serial is accepted, because every daemon start re-verifies the installed bundle.

- `scripts/build_org_bundle.py --sign-key` writes the serial; `--serial N` sets it, the default is the
  current Unix time. `--serial` without `--sign-key` is an argument error. `--merge` drops the old
  serial so a merged bundle gets a new one.
- `verify_and_maybe_pin` in `src/privacyfence/org_bundle_signing.py` runs the serial check after the
  signature verified, on both the pinned-key and the first-pin path, and on the first-pin path before
  the key is pinned, so a bundle refused for its serial pins no key. It never writes the serial pin.
- `record_accepted_serial` writes the pin, only after a caller has succeeded:
  `daemon_main.load_org_config` after its checks, and `SettingsController.install_org_config_bytes`
  after the file is written. It never raises; a failed write is logged and the caller carries on.
- A malformed serial, an unreadable pin file, a missing serial once one is pinned, and a lower serial
  are each refused with their own message. The message for the last three names the file an
  administrator deletes to deliberately go back.
- Unsigned bundles and installs with no key pinned are unchanged.

## Alternatives considered

- **An `issued_at` timestamp compared with the clock.** A clock is not a trust anchor, and the point
  is ordering, not freshness.
- **Record the serial inside `verify_and_maybe_pin`.** It is also called for a bundle that is then
  not written; the pin would run ahead of the installed file and refuse the old file at the next start.
- **Keep the pin inside `org_config.json`.** ADR 0016's reasoning for keeping the key pin outside the
  bundle applies unchanged.
- **Refuse serial-less bundles outright.** It breaks every existing signed install on upgrade.

## Consequences

- A signed bundle without a serial is accepted until a serial is pinned. An install that has not
  rebuilt its bundle keeps working, and so is not protected until it installs one with a serial; from
  then on a serial-less bundle is refused.
- Key rotation (deleting the key pin) keeps the serial pin; the new bundle's timestamp serial is
  higher. Deleting both files is the full reset, and the deliberate way back to an older bundle.
- The pin file is as trustworthy as the install directory: someone who can delete it can roll back.
  That is the same boundary the key pin has.
- An administrator who sets `--serial` by hand must keep it increasing.

## Verification

- `tests/unit/test_org_bundle_signing.py`: serial validation, ordering, equal accepted, no pin on refusal,
  pin file errors, `record_accepted_serial`.
- `tests/unit/test_build_org_bundle.py`: `--serial`, its default, the argument error and `--merge`.
- `tests/unit/test_settings_controller.py`: the serial is recorded only after the file is written.

## Related

- Issue [#924](https://github.com/privacyfence/privacyfence/issues/924).
- Amends [ADR 0016](0016-org-config-bundle-hash-log-and-signing.md).
