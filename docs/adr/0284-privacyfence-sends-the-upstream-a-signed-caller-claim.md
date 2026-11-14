# ADR 0284: PrivacyFence sends the upstream a signed caller claim

## Status

Accepted — 2026-11-14. Amends [ADR 0036](0036-card-copy-names-the-caller-through-one-placeholder.md).
Implemented: `src/privacyfence/upstream_claims.py`, `web/routes_upstream_mcp.py`.

## Context

An upstream MCP server (ADR 0283) must filter its own data by who is asking: ranking and pagination stay
correct only when the filter is inside its queries, and a guessed document id must be checked where the
data lives. PrivacyFence is the one place that knows the person, their policy and what is calling, so it
has to tell the upstream, in a form the upstream can verify and not forge. [ADR
0036](0036-card-copy-names-the-caller-through-one-placeholder.md) kept connectors from learning who is
asking.

## Decision

Every HTTP request to an upstream carries `Authorization: Bearer <jwt>`, an RS256 token PrivacyFence signs
with its own key (`upstream_claim_signing_key.pem` in the org directory, created on first start) and
publishes at `/upstream-mcp/jwks`. The header `typ` is `pf-upstream-claim+jwt`. The payload has `iss`
(`<issuer>/upstream-mcp`), `aud` (the upstream's declared audience), `sub`, `email`, `email_verified`,
`domains`, `act`, `pf_upstream`, `pf_tool`, `iat`, `nbf`, `exp` (`iat` + 60 seconds) and `jti`. A fresh
claim is minted per HTTP request, never cached or shared across callers.

`act` is `{"type": "workflow", ...}` for a scoped token (checked first), `{"type": "ai_client",
"attested", "name", "claimed_name"}` otherwise (a name is put in `name` only when attested, else in
`claimed_name`), and `{"type": "service"}` with `sub` `privacyfence` and no domains for the startup tool
list. Only `sub`, `domains` and `act.type` may change what an upstream returns; the rest is for its audit
([ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) decision 3).

The claim is built beside the transport (`upstream_mcp_runtime.caller_for` supplies it to the client),
so connector code still never reads the caller. The amendment to ADR 0036 is that identity now crosses
the boundary 0036 kept closed, in this one signed form, to a server the operator named.

## Alternatives considered

- **Reuse the OIDC signing key** (ADR 0274) — ties the feature to `PRIVACYFENCE_OIDC_CLIENTS`, lets a
  claim be confused with a Peek or Grist ID token on one JWKS, and couples key rotation.
- **A custom header instead of a bearer token** — MCP servers already verify bearer tokens.
- **A longer lifetime or caching claims** — a revoked grant must stop working within a minute.
- **Let connector code read the caller** — would undo ADR 0036 for every connector.
- **Filter the upstream's results in PrivacyFence** — cannot keep ranking and pagination right, and cannot
  check a guessed id where the data lives.

## Consequences

- The upstream must verify `typ`, `aud`, `iss`, `exp` and `jti`; PrivacyFence cannot enforce it. The doc
  ([`docs/upstream-mcp.md`](../upstream-mcp.md)) and the reference server are the contract.
- Rotating the key is deleting the file; the next start publishes a new `kid`.
- The audit log does not yet record the claim's `jti`; correlating with the upstream's log is a later change.
- A third `act` value (`service`) exists beyond the two an owner decided on; it carries no person and no
  domains.

## Verification

- `tests/unit/test_upstream_claims.py` (header, payload, lifetime, `jti` per mint, audience, the actor
  shapes, one claim per request).
- `tests/unit/web/test_routes_upstream_mcp.py` (the JWKS route).
- `tests/unit/test_upstream_mcp_reference_server.py` (an upstream rejecting a wrong `typ`, `aud`, `iss`,
  expired claim and a replayed `jti`).
- `tests/integration/test_upstream_mcp_org.py`.

## Related

[ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md),
[ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md),
[ADR 0274](0274-the-oidc-signing-key-is-generated-in-the-org-directory.md),
[ADR 0283](0283-upstream-mcp-servers-are-declared-connectors.md).
