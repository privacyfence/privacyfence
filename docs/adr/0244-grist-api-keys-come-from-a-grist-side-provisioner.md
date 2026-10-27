# ADR 0244: Grist API keys come from a Grist-side provisioner that PrivacyFence only calls

## Status

Accepted — 2026-10-27. Implemented.
Relates to 0142, 0143.

## Context

ADR 0142 lets an organization offer Grist with a personal API key when its bundle carries no OAuth app,
and ADR 0143 pins the Grist server in the bundle. The free grist-core edition has no OAuth apps, and
Grist allows exactly one API key per person. So every person had to open Grist, create a key under
Account settings → Developer → API Key and paste it into /connect. Grist's home database is
reachable only inside Grist's own network zone.

grist-core has no API that creates a key for another user. Whoever can write Grist's home database
can act as any Grist user, so that power must stay in Grist's zone.

## Decision

1. A small provisioner service, [`privacyfence/grist-provisioner`](https://github.com/privacyfence/grist-provisioner),
   runs next to Grist and sets or reads a person's key directly in Grist's database. PrivacyFence only
   calls it over HTTP (`GristProvisionerClient` in [`grist_provisioner_api.py`](../../src/privacyfence/grist_provisioner_api.py)); the provisioner
   never connects to PrivacyFence.
2. PrivacyFence authenticates with a scoped provisioning key that starts with `gristprov_`. The
   provisioner refuses the Grist install admin with that key. The key is never in the bundle: the
   bundle's `grist.auto_provision.key_env` names the environment variable the daemon reads
   (`GristProvisioningConfig` in [`org_mode.py`](../../src/privacyfence/org_mode.py)).
3. Auto-provisioning is for API-key set-ups only: `from_org_config` refuses it when the bundle carries a
   Grist OAuth `client_id`, and requires organization mode and a `grist.server_url`.
4. Provisioning runs on a background worker after a sign-in, never during it
   ([`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py)), so PrivacyFence sign-in never waits on Grist or the provisioner.
5. Offboarding is `privacyfence-app --grist-release PRINCIPAL_ID`.

## Alternatives considered

- **Forward auth (`GRIST_FORWARD_AUTH_HEADER`) to call `/api/profile/apikey` as each person.** In
  grist-core it is a login system that replaces Grist's own Google sign-in, so the organization would have
  to move Grist's sign-in behind a proxy such as oauth2-proxy, and any caller that can set the header can
  act as anyone.
- **A Grist OAuth app.** Not in the free edition.
- **SCIM (`GRIST_ENABLE_SCIM`).** It can create users but issues no credential. Creating accounts was
  declined: Grist creates them at first sign-in or on sharing, and an empty account reaches nothing. It
  would also put an admin key into the provisioner.
- **One shared Grist service account.** It drops each person's own Grist access rules.
- **The provisioner, or Grist's boot key, inside PrivacyFence.** Whoever writes Grist's database can act
  as any Grist user; that power stays in Grist's zone, behind a scoped key that refuses the install admin.
- **Provisioning during sign-in, or for everyone at startup.** The first would make sign-in wait on Grist;
  the second cannot work, because emails are only known after a sign-in.

## Consequences

- Each person who signs in and has a Grist account is connected with no clicks, and an outage of Grist or
  the provisioner only delays provisioning.
- The organization runs one more small service next to Grist and keeps its provisioning key in the
  daemon's environment.
- A person without a Grist account yet is retried by the sweep until Grist has created one.
- People on an OAuth-app set-up are unaffected: the combination is refused at startup.

## Verification

- `tests/unit/test_grist_provisioning.py`, `tests/unit/test_grist_provisioner_api.py`.
- `GristProvisioningConfig.from_org_config` ([`org_mode.py`](../../src/privacyfence/org_mode.py)) refuses a broken section at startup.

## Related

- ADRs [0142](0142-grist-connects-by-oauth-with-an-app-in-the-bundle-and-by-api-key-otherwise.md), [0143](0143-the-bundle-pins-the-grist-server-and-a-credential-stays-with-its-server.md)
- The provisioner: <https://github.com/privacyfence/grist-provisioner>
- Source: [`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py), [`grist_provisioner_api.py`](../../src/privacyfence/grist_provisioner_api.py), [`grist_auth.py`](../../src/privacyfence/grist_auth.py), [`org_mode.py`](../../src/privacyfence/org_mode.py)
