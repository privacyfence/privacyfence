# ADR 0297: The upstream claim is an EdDSA identity claim in its own header, bound to one call

## Status

Accepted — 2026-11-14. Amends [ADR 0286](0286-privacyfence-sends-the-upstream-a-signed-caller-claim.md).
Implemented: `src/privacyfence/identity_claim.py`, `src/privacyfence/upstream_claim_keys.py`,
`web/routes_upstream_mcp.py`, `contrib/verifier/pf_claim_verify.py`. Issue
[#1103](https://github.com/privacyfence/privacyfence/issues/1103).

## Context

ADR 0286 sent the upstream an RS256 bearer token in `Authorization`, valid for 60 seconds and good for any
request. That has three gaps. A claim captured from one request can be replayed against another, so it
proves who is asking but not what was asked. The header is the one an upstream may need for its own
credential. And one key file that is rebuilt by deleting it cannot be rotated without a gap in which an
upstream's cached key set rejects every claim. Nothing was released with the old claim, so it can be
replaced rather than kept beside a new one.

## Decision

The claim is a compact JWT in its own header, `X-PrivacyFence-Claim`, on every HTTP request to the upstream;
`Authorization` is never set or read. The header is `{"alg": "EdDSA", "kid", "typ": "pf-identity-claim+jwt"}`
and the payload carries `claim_version` 1, `iss`, `aud`, `sub`, `principal_type` (`claude_client`,
`n8n_workflow`, `service`; `human` is reserved), `email` (only when `sub` is a person), `approved_by` and
`token_id` (only for a scoped token), `act` (RFC 8693: the OAuth client id and the attested agent id), sorted
`audiences` and `tools`, `req`, `iat`, `exp` and `jti`. A key is omitted, never null. Two values differ from
the claim it replaces: `iss` is the issuer URL with no `/upstream-mcp` suffix, and `jti` is a UUID string, not
hex. The lifetime is the upstream's `claim_lifetime_seconds` (5 to 60, default 60).

**One call.** On a `tools/call` request `req` is the base64url SHA-256 of the canonical JSON of
`{"tool", "arguments"}` (the upstream's own tool name and the arguments as sent, without `reason`; sorted
keys, UTF-8, no spaces). Every other request (`initialize`, `notifications/initialized`, the session delete)
carries `audiences: []`, `tools: []` and no `req`. A verifier refuses a `tools/call` without a matching
`req`, a `req` on anything else and a JSON-RPC batch, so a captured claim cannot carry audiences to a
different call. `params._meta` is not bound; an upstream must dispatch only the tool name and arguments it
hashed.

**Policy per upstream** in the operator file: `identity_claim` (`required`, the default, or `off`),
`claim_lifetime_seconds` and `allowed_principal_types`. A caller of a type that is not allowed is refused
before anything is sent; a signing failure sends nothing; an unresolved membership sends `audiences: []`.

**Keys.** Ed25519, kept in `upstream_claim_keys.json` in the org directory (mode 0600, written atomically,
read-modify-write under a file lock), published at the unchanged path `/upstream-mcp/jwks`. Rotation
(`--rotate-upstream-claim-key`) publishes the new key at once and starts signing with it 300 seconds later,
the JWKS cache lifetime, so a verifier has it before it is used. The previous key stays in the key set for
`claim.key_overlap_hours` (default 24) and its private seed is dropped once it stops signing.
`--revoke-upstream-claim-key KID` removes a key at once and refuses the only signing key. The old
`upstream_claim_signing_key.pem` is ignored and never deleted.

**Audit.** Every call forwarded to an upstream writes one `upstream_forwarded` entry with the claim's `jti`,
`sub`, type, audiences, `act` and `req` and the outcome, so the upstream's log can be matched to
PrivacyFence's. The claim itself, argument values and the upstream's text never reach the log.

The contract is `docs/identity-claim.md`; `contrib/verifier/pf_claim_verify.py` is a single file an upstream
can vendor.

## Alternatives considered

- **Keep both claims** — the RS256 bearer and the new one side by side. Nothing was released, so it only
  doubles what an upstream must verify and what a reviewer must check.
- **Hash all of `params`** — would also bind `_meta`, but the owner fixed the request format as
  `{tool, arguments}` and `_meta` is client-supplied; the contract tells an upstream to dispatch on what it
  hashed.
- **A claim without `req` for non-call requests carrying audiences** — the hole the `req` rules close.
- **Sign the new key in at once on rotation** — a verifier with a cached key set rejects every claim until
  its cache expires.
- **Keep the RS256 key and the bearer header** — occupies the header an upstream needs for its own credential.

## Consequences

- An upstream must verify `typ`, `aud`, `iss`, `exp`, `claim_version`, `req` and the `jti`; PrivacyFence
  cannot enforce it. A verifier that follows the contract returns no audiences for a request that is not a call.
- Python's shortest float representation is the one place another language can differ in `req`; the contract
  lists test vectors for it.
- The new key signs five minutes after a rotation, not at once.
- Anyone who can read the key file can sign claims; it is mode 0600 beside the other org secrets.

## Verification

- `tests/unit/test_identity_claim.py` (payload, `req`, per-request signing, refusal and fail-closed cases).
- `tests/unit/test_upstream_claim_keys.py` (rotation, overlap, revoke, locking, JWKS).
- `tests/unit/test_pf_claim_verify.py` (the verifier against the written contract, with the vectors in
  `contrib/verifier/req_test_vectors.json`).
- `tests/unit/test_upstream_mcp_examples.py` and `tests/unit/test_upstream_mcp_reference_server.py`.
- `tests/integration/test_upstream_mcp_org.py`.

## Related

[ADR 0036](0036-card-copy-names-the-caller-through-one-placeholder.md),
[ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md),
[ADR 0286](0286-privacyfence-sends-the-upstream-a-signed-caller-claim.md).
