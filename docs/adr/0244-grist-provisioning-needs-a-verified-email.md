# ADR 0244: Grist provisioning needs a verified email

## Status

Accepted — 2026-10-27. Implemented.
Relates to 0142, 0143.

## Context

ADR 0142 lets an organization offer Grist with a personal API key when its bundle carries no OAuth app,
and ADR 0143 pins the Grist server in the bundle. The free grist-core edition has no OAuth apps, and
Grist allows exactly one API key per person. So every person had to open Grist, create a key under
Account settings → Developer → API Key and paste it into /connect. Grist's home database is
reachable only inside Grist's own network zone.

The provisioner finds the Grist account by email. PrivacyFence's principal copies `claims["email"]`
without reading `email_verified`. Without a check, anyone who can set their own email at the identity
provider could be handed another person's Grist key.

## Decision

1. Only a principal whose ID token has `email_verified` set to the JSON `true` or the string `"true"` is
   provisioned (`email_verified_claim` in [`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py)); otherwise the status is
   `unverified_email` and the provisioner is not called.
2. The bundle option `grist.auto_provision.trust_unverified_email` (`GristProvisioningConfig` in
   [`org_mode.py`](../../src/privacyfence/org_mode.py)) turns the check off, for identity providers that never send the claim but issue
   only verified, organization-owned addresses. It is off by default and must be given again on every
   `--grist-auto-provision` rebuild.
3. The email is stored in the person's state file at sign-in, so that renewal and the sweep can run
   without a sign-in. `--grist-release` clears it.

## Alternatives considered

- **Trust the email claim always.** Unsafe where users can set their own address.
- **Require a verified email with no escape.** It would lock out providers that never send the claim.
- **Keep the email only in the session.** Renewal runs between sign-ins and needs it.

## Consequences

- Google, which always sends the claim, works unchanged.
- An organization using an identity provider without the claim has to opt in knowingly.
- A stored email sits in the person's own directory; it is not logged, and `released` empties it.

## Verification

- `tests/unit/test_grist_provisioning.py`, `tests/unit/test_org_mode.py`.

## Related

- ADRs [0142](0142-grist-connects-by-oauth-with-an-app-in-the-bundle-and-by-api-key-otherwise.md), [0143](0143-the-bundle-pins-the-grist-server-and-a-credential-stays-with-its-server.md)
- The provisioner: <https://github.com/privacyfence/grist-provisioner>
- Source: [`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py), [`grist_provisioner_api.py`](../../src/privacyfence/grist_provisioner_api.py), [`grist_auth.py`](../../src/privacyfence/grist_auth.py), [`org_mode.py`](../../src/privacyfence/org_mode.py)
