# ADR 0245: A managed Grist key expires 90 days after it was issued

## Status

Accepted — 2026-10-27. Implemented.
Relates to 0142, 0143.

## Context

ADR 0142 lets an organization offer Grist with a personal API key when its bundle carries no OAuth app,
and ADR 0143 pins the Grist server in the bundle. The free grist-core edition has no OAuth apps, and
Grist allows exactly one API key per person. So every person had to open Grist, create a key under
Account settings → Developer → API Key and paste it into /connect. Grist's home database is
reachable only inside Grist's own network zone.

A managed key is plaintext in Grist's database and, once PrivacyFence has it, anyone with it can act as
the person. People leave, and PrivacyFence may not be told. Rotation is not an option (ADR 0243).

## Decision

1. The provisioner sets `expires_at` to 90 days after the key was last issued and deletes an expired
   managed key in Grist.
2. PrivacyFence renews a managed key 30 days before expiry (`RENEW_BEFORE_SECONDS`), by asking the
   provisioner for the key again, which pushes `expires_at` out and returns the same key
   ([`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py)).
3. A six-hourly sweep re-queues people whose last sign-in is within 90 days (`INACTIVE_AFTER_SECONDS`).
   After that PrivacyFence stops renewing, and the key lapses.
4. So a managed key lives at most about 180 days after the person's last sign-in (a renewal near day 89
   lasts to about day 180). For anything sooner, `privacyfence-app --grist-release PRINCIPAL_ID` deletes it
   in Grist right away.

## Alternatives considered

- **Never expire.** A leaver's key would live as long as the Grist account.
- **Rotate on a schedule.** It would break anything the person copied the key into.
- **Expire at the last sign-in, or shortly after.** A holiday would disconnect the person.

## Consequences

- A leaver's managed key stops working on its own, without anybody remembering to release it.
- A person absent for more than 90 days is reconnected at the next sign-in.
- The window is not zero: an organization needing immediate removal runs `--grist-release`, after
  removing the person from the identity provider (a new sign-in provisions them again).

## Verification

- `tests/unit/test_grist_provisioning.py` (renewal, sweep, inactivity), `tests/unit/test_grist_provisioner_api.py`.

## Related

- ADRs [0142](0142-grist-connects-by-oauth-with-an-app-in-the-bundle-and-by-api-key-otherwise.md), [0143](0143-the-bundle-pins-the-grist-server-and-a-credential-stays-with-its-server.md)
- The provisioner: <https://github.com/privacyfence/grist-provisioner>
- Source: [`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py), [`grist_provisioner_api.py`](../../src/privacyfence/grist_provisioner_api.py), [`grist_auth.py`](../../src/privacyfence/grist_auth.py), [`org_mode.py`](../../src/privacyfence/org_mode.py)
