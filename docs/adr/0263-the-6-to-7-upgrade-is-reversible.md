# ADR 0263: The 6 to 7 upgrade is reversible

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/org_bundle_signing.py` (`adopt_legacy_files`), `src/privacyfence/paths.py`.
Amends [ADR 0016](0016-org-config-bundle-hash-log-and-signing.md) and [ADR 0200](0200-an-organization-bundle-carries-a-signed-serial-and-an-older-one-is-refused.md): the bundle and pin file names.
Relates to [ADR 0262](0262-7x-is-the-migration-checkpoint-for-8x.md).

## Context

7.x changes how organization servers are configured. An operator who upgrades and finds a problem
must be able to go back to the latest 6.x (6.10.0 or newer, the first release that reads the data
formats 7.x writes) and keep running with the `org_config.json` it had. If 7.x installed a newer
bundle and advanced the serial pin that 6.x also reads
([ADR 0200](0200-an-organization-bundle-carries-a-signed-serial-and-an-older-one-is-refused.md)), 6.x
would refuse its own older bundle after the downgrade.

## Decision

- **7.x never writes, renames or deletes a 6.x bundle or pin file.** It reads and writes new names:
  `org/org_bundle.json`, `org/org_bundle_signing_pubkey.txt`, `org/org_bundle_serial.txt`
  (the 6.x names are `org_config.json`, `org_config_signing_pubkey.txt`, `org_config_serial.txt`).
- **Adoption is a copy.** `adopt_legacy_files` runs at the top of `load_org_config`: when
  `org_bundle.json` does not exist and `org_config.json` does, it copies the bytes unchanged (so the
  signature still verifies), copies each legacy pin whose new name is missing, records the copy in
  `org/org_bundle_adopted.json` (source, SHA-256, time), logs a warning and writes an
  `org_bundle_adopted` audit entry. A failed copy refuses the start rather than run with half a
  configuration.
- **Pins are copied only until adoption records `pins_adopted`.** Afterwards, deleting the new-name pin
  (the documented key rotation) never brings the old key back.
- **After adoption, an edit to `org_config.json` is ignored with a warning** at every start, so an
  operator who follows old instructions is not met with silence.
- **The other 7.x writes are 6.x-compatible.** New files (`admin_config.json`, `data_format.json`,
  `relay_token.txt`, `org_bundle_adopted.json`) are ignored by 6.x, and the rewrites of rules and
  tokens produce formats 6.10 and newer read.
- **What does not go back:** changes made on the Admin page, bundles installed under 7.x, connections
  made through the relay, and passkeys or connections made in single-user mode. A downgrade returns to
  the configuration `org_config.json` holds.
- **Re-upgrading.** `org_bundle.json` and `admin_config.json` still exist, so 7.x uses them as they
  were. To start from 6.x's current files, delete `org_bundle.json`, `org_bundle_adopted.json`, the two
  new pin files and `admin_config.json` before starting 7.x. Deleting only `org_bundle.json` re-adopts
  the old file, and a newer serial pin refuses it.
- Either pin name counts as "this directory belonged to an organization server"
  ([ADR 0260](0260-organization-mode-can-start-from-the-environment-and-run-single-user-on-localhost.md)).

## Alternatives considered

- **Renaming `org_config.json` in place.** Rejected: it breaks a downgrade at once.
- **Sharing the pin files.** Rejected: a 7.x serial would refuse 6.x's older bundle.
- **Reading whichever file is newer.** Rejected: which one wins would depend on file times, and an
  operator copying a bundle to the old name by habit would silently change what 7.x runs.

## Consequences

- The data directory holds two copies of the bundle until 8.x, which deletes the adoption code and
  keeps the "ignored" warning.
- The documentation names the exact files to delete and the release to return to.

## Verification

`tests/unit/test_org_bundle_signing.py`, `tests/unit/test_daemon_main.py`. Code:
`src/privacyfence/org_bundle_signing.py`, `src/privacyfence/daemon_main.py` (`load_org_config`),
`deploy/docker/healthcheck.py`.

## Related

ADR 0016, 0200, 0260, 0262, 0269.
