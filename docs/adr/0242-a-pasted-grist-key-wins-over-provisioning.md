# ADR 0242: A Grist key the person pasted on /connect wins over provisioning

## Status

Accepted — 2026-10-27. Implemented.
Relates to 0142, 0143.

## Context

ADR 0142 lets an organization offer Grist with a personal API key when its bundle carries no OAuth app,
and ADR 0143 pins the Grist server in the bundle. The free grist-core edition has no OAuth apps, and
Grist allows exactly one API key per person. So every person had to open Grist, create a key under
Account settings → Developer → API Key and paste it into /connect. Grist's home database is
reachable only inside Grist's own network zone.

Once auto-provisioning exists, a person can still paste their own key on /connect, and a sign-in or the
sweep can run at the same moment.

## Decision

1. A key pasted on /connect is saved as a *manual* credential file. Only a provisioned file carries
   `"source": "provisioned"` (with `managed` and `expires_at`); a manual file stays byte-identical to
   what it was before ([`grist_auth.py`](../../src/privacyfence/grist_auth.py): `save_api_key`, `token_source`).
2. `provision_principal` ([`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py)) leaves a usable manual file alone: it records the
   status `user_connected` and does not call the provisioner.
3. `/connect` and the worker take one per-principal lock (`principal_lock`) around the check and the save,
   and the worker re-reads the file under that lock before saving, so a paste that lands in between is never
   overwritten. The lock is never held across a provisioner call.
4. `/connect` tells a person whose connection is provisioned so, and keeps the API-key form.

## Alternatives considered

- **Provisioning replaces a manual key.** It would undo a choice the person made on purpose.
- **Remove the paste form once provisioning is on.** People whose Grist account does not exist yet, or who
  want a key of their own, would have no way to connect.
- **No lock; last write wins.** A paste could be lost to a worker that read the file before it.

## Consequences

- A person can always override provisioning by pasting a key; provisioning never takes it back.
- A manual file is never renewed, expired or deleted by PrivacyFence's provisioning.
- Code that writes the Grist credential file for a provisioned key must go through the same lock.

## Verification

- `tests/unit/test_grist_auth.py` (manual file unchanged), `tests/unit/test_grist_provisioning.py`,
  `tests/unit/web/test_routes_connect.py` (`TestGristApiKey`).

## Related

- ADRs [0142](0142-grist-connects-by-oauth-with-an-app-in-the-bundle-and-by-api-key-otherwise.md), [0143](0143-the-bundle-pins-the-grist-server-and-a-credential-stays-with-its-server.md)
- The provisioner: <https://github.com/privacyfence/grist-provisioner>
- Source: [`grist_provisioning.py`](../../src/privacyfence/grist_provisioning.py), [`grist_provisioner_api.py`](../../src/privacyfence/grist_provisioner_api.py), [`grist_auth.py`](../../src/privacyfence/grist_auth.py), [`org_mode.py`](../../src/privacyfence/org_mode.py)
