# ADR 0241: PrivacyFence provisions Peek accounts with a scoped key and only ever calls Peek

## Status

Accepted — 2026-10-27. Implemented.
Extends 0151, 0152.

## Context

ADR 0151 has each person connect Peek by hand with Peek's device sign-in, and ADR 0152 ties the
token to the Peek server it was issued for. In organization mode that is one click-through per
person. An organization wants every person who signs in to PrivacyFence to have a Peek account
without it. PrivacyFence and Peek run in separate DMZs, so whichever side starts the connection
matters. Peek's provisioning API (a scoped `peekprov_` key; `PUT` an account, mint a token,
`DELETE` to release) is described at
<https://github.com/andras-tkcs/peek/blob/main/docs/src/content/docs/auth-access.md>.

## Decision

1. **Only PrivacyFence calls Peek.** Peek never opens a connection into PrivacyFence.
2. **A scoped provisioning key, not an admin key.** The key is read from an environment variable
   whose name the signed bundle carries (`--peek-provisioning-key-env`); the key itself is never in
   the bundle. It can create and token non-admin accounts and nothing more.
3. **Provisioning starts at sign-in and never blocks it.** A sign-in callback queues the person; one
   background worker (`peek_provisioning.py`) makes the Peek calls, paced, with a periodic sweep.
   A slow or down Peek only delays provisioning.
4. **Admins are never adopted.** Peek answers 409 for a Peek admin account; the person connects it by
   hand (ADR 0151).
5. **Release revokes only PrivacyFence's token.** `privacyfence-app --peek-release` calls `DELETE`
   and clears the stored email. It does not disable the Peek account.

See `src/privacyfence/peek_provisioning.py`, `src/privacyfence/peek_client.py` and
`src/privacyfence/org_mode.py`.

## Alternatives considered

- **Peek pulls the user list from PrivacyFence, or PrivacyFence exposes a webhook.** Peek must not
  open connections into PrivacyFence's DMZ.
- **An ordinary Peek admin API key in PrivacyFence.** A compromised PrivacyFence host would be a
  full Peek admin. The scoped key never reaches an admin account.
- **The key in the signed bundle.** Bundles are copied around, for the same reason the SIEM bearer
  token stays out of them.
- **Provision synchronously during sign-in.** A slow or down Peek would delay or break PrivacyFence
  sign-in.
- **Provision every principal at startup without a sign-in.** Not possible: emails are not stored
  until a person signs in once after this lands.
- **Disable the Peek account on release.** That is the Peek admin's decision, and the account may
  predate PrivacyFence.

## Consequences

- Each product keeps working without the other; Peek keeps its own users and sign-in.
- A compromised PrivacyFence host exposes a key limited to non-admin accounts.
- A released person's Peek account stays; only the token PrivacyFence minted is revoked.
