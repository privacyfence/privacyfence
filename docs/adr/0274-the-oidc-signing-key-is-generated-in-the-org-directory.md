# ADR 0274: The OIDC signing key is generated in the org directory

## Status

Accepted — 2026-10-31. Implemented: `load_or_create_signing_key` in `src/privacyfence/oidc_provider.py`.
Relates to [ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md).

## Context

The OIDC provider signs ID tokens, and Peek and Grist verify them against the key set at
`/oidc/jwks`. The key has to survive restarts, and the operator should not have to make one.

## Decision

- **RS256 with an RSA 2048 key,** generated with `cryptography` on the first start that has
  `PRIVACYFENCE_OIDC_CLIENTS` set, and written atomically (temporary file, mode `0600`, `os.replace`)
  to `org/oidc_signing_key.pem` as PKCS8 PEM.
- **The `kid`** is the first 16 characters of the unpadded base64url SHA-256 of the public key's DER
  SubjectPublicKeyInfo.
- **A file that exists but is unusable** (unreadable, not PEM, not RSA, or under 2048 bits) refuses
  the start and names the file; it is never silently replaced.
- **Rotation is deleting the file.** The next start makes a new key. ID tokens last 5 minutes, so no
  overlap of old and new keys is kept.

## Alternatives considered

- **ES256 or EdDSA.** Smaller, but not every OIDC client library accepts them by default; `openid-client`
  (Grist) defaults to RS256 and `go-oidc` (Peek) accepts it.
- **A key supplied by the operator in the environment.** One more secret to generate, store and rotate,
  for no gain over a file in the directory PrivacyFence already owns.
- **A rotation schedule with overlapping keys.** Tokens live 5 minutes and the apps keep their own
  sessions, so the complexity buys nothing.

## Consequences

- Restoring `org/` from a backup restores the key; losing it only invalidates tokens at most 5 minutes
  old.
- Apps refetch the key set when they see an unknown `kid`; an app that caches it for longer fails
  sign-ins until it refetches.

## Verification

`tests/unit/test_oidc_provider.py` (key created once with mode `0600`, reused, unusable file refused,
stable `kid`). Operator documentation: `docs/oidc-provider.md`.

## Related

ADR 0273.
