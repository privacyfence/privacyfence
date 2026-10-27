# ADR 0242: A person's own Peek sign-in wins over provisioning

## Status

Accepted — 2026-10-27. Implemented.
Relates to 0151, 0241.

## Context

A person can connect Peek by hand (ADR 0151), and an organization can provision an account for them
(ADR 0241). Both write the same per-principal token file, `credentials/peek_token.json`. Without a
rule, provisioning could overwrite a sign-in the person chose, and a Peek admin account can only be
connected by the person's own sign-in. Peek's sign-in is described at
<https://github.com/andras-tkcs/peek/blob/main/docs/src/content/docs/auth-access.md>.

## Decision

1. `peek_token.json` carries a `source` marker: `"provisioned"` (with `expires_at`) or, by default,
   `"device"`. Device files stay byte-identical to before.
2. Provisioning never replaces a `device` file; it records the status `user_connected` and does not
   call Peek.
3. A person's device sign-in through **Reconnect Peek** replaces a provisioned file.
4. The `/connect` Peek save and provisioning share one per-principal lock, so they cannot write the
   file at the same moment.
5. `/connect` says "Your organization connected your Peek account automatically." for a provisioned
   file.

See `src/privacyfence/peek_provisioning.py` and `src/privacyfence/peek_client.py`.

## Alternatives considered

- **Overwrite a person's own device sign-in.** Their choice wins, and an admin account can only be
  connected that way.

## Consequences

- A person can always take over their own Peek connection.
- The provisioned token stays live in Peek until it expires; this is documented.
