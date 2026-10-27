# ADR 0246: An existing Grist key is adopted and never revoked by PrivacyFence

## Status

Accepted — 2026-10-27. Implemented.
Relates to 0142, 0143.

## Context

ADR 0142 lets an organization offer Grist with a personal API key when its bundle carries no OAuth app,
and ADR 0143 pins the Grist server in the bundle. The free grist-core edition has no OAuth apps, and
Grist allows exactly one API key per person. So every person had to open Grist, create a key under
Account settings → Developer → API Key and paste it into /connect. Grist's home database is
reachable only inside Grist's own network zone.

Because Grist keeps one key per person, a person who already made a key has nothing to spare: handing
PrivacyFence a new key would replace theirs, and the old one would stop working in their own scripts.

## Decision

1. When the account has no key, the provisioner creates one, called *managed*. When the person already
   made a key, the provisioner *adopts* it and returns it as it is (`managed: false`, `expires_at: 0`).
2. PrivacyFence records which it holds in the credential file's `managed` flag and in the state status
   (`provisioned` or `adopted`) ([`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py), [`grist_auth.py`](../../src/privacyfence/grist_auth.py)).
3. PrivacyFence never deletes an adopted key. On release, the provisioner deletes a key only if it created
   it and it is still the account's current key; the answer's `revoked` flag says whether that happened
   ([`grist_provisioner_api.py`](../../src/privacyfence/grist_provisioner_api.py): `release_account`).
4. `key_id(api_key)` is the first 12 hex characters of the key's SHA-256. PrivacyFence compares it with
   the provisioner's `key_id` to see whether its copy is still the person's current key, without the
   provisioner sending the key again.

## Alternatives considered

- **Force a fresh key for people who already have one.** It would break their own scripts.
- **Rotate managed keys.** Grist has one key per person, and a rotation would break anything the person
  copied from Grist's settings page.
- **Delete the key on release whatever its origin.** For an adopted key that is the person's own.

## Consequences

- Nobody loses a working Grist key because PrivacyFence arrived.
- After `--grist-release` of an adopted key, the key stays in Grist; the command says so, and the
  organization disables the account in Grist if the key must go.
- If the person regenerates their key in Grist, the `key_id` no longer matches and PrivacyFence fetches
  the new one at the next sign-in or sweep.

## Verification

- `tests/unit/test_grist_provisioning.py`, `tests/unit/test_grist_provisioner_api.py`,
  `tests/unit/test_grist_auth.py`.

## Related

- ADRs [0142](0142-grist-connects-by-oauth-with-an-app-in-the-bundle-and-by-api-key-otherwise.md), [0143](0143-the-bundle-pins-the-grist-server-and-a-credential-stays-with-its-server.md)
- The provisioner: <https://github.com/privacyfence/grist-provisioner>
- Source: [`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py), [`grist_provisioner_api.py`](../../src/privacyfence/grist_provisioner_api.py), [`grist_auth.py`](../../src/privacyfence/grist_auth.py), [`org_mode.py`](../../src/privacyfence/org_mode.py)
