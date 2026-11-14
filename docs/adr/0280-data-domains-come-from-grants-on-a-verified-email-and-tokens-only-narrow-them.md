# ADR 0280: Data domains come from grants on a verified email, and scoped tokens only narrow them

## Status

Accepted — 2026-11-14. Amends [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)
and [ADR 0236](0236-a-scoped-token-holds-its-own-rules.md). Implemented:
`upstream_mcp_config.py` (`GrantStore`, `domains_for`), `principal.py`, `scoped_tokens.py`.

## Context

The upstream (ADR 0278) filters by the data domains in the claim (ADR 0279). Something has to decide
which domains a person holds. `Principal.email` is cosmetic, and an unverified address must not unlock
data. A scoped token (ADR 0164, 0236) is a deny-only credential: it can do less than its owner, never more.

## Decision

Domains come from **grants** in `admin_config.json` under `upstream_mcp.<id>.grants`: each grant matches
`*`, `@<domain>` or one email address and lists domains; a person holds the union of the grants that match
them. Grants are re-read when the file changes, so an edit applies to the next call without a restart.
Grants match only an email PrivacyFence treats as **verified**: the identity provider said so, or the
operator set `trust_unverified_email` and the address is in an allowed domain
(`org_identity.session_email_verified`). The bit is carried on the MCP principal as
`Principal.email_verified`, in access-token claims and refresh records; a missing key decodes as
`False`, so older sessions give no domains until the person signs in again. The single-user person is
verified ([ADR 0272](0272-a-single-user-install-provisions-its-one-person-as-verified.md)).

A **scoped token** carries `upstream_domains` per upstream, chosen when it is made, and
`principal_email_verified`. Its call gets the domains its owner holds *now*, intersected with the token's
list. Creating a token with a domain its creator does not hold is refused. A token made without ticking any
domain sees none, and tokens made before this change send none.

## Alternatives considered

- **Domains from identity-provider group claims** — varies by provider and is not what the owner chose.
- **Trust any email address** — an unverified address would unlock another person's data.
- **A token that carries its own full domain list** — it could outlive a revoked grant and could widen.
- **An Admin page control for grants** — a file edit is enough for this release.

## Consequences

- A grant for a domain unlocks data only for a verified address; an organization that sets
  `trust_unverified_email` for a domain accepts that its unverified addresses can unlock it.
- A revoked grant stops working within the claim's 60 seconds, for people and tokens alike.
- Scoped-token rules for upstream tools are a later change; the New token form lists only `auto` upstream
  tools, with a domain fieldset per upstream.
- AI token proposals (ADR 0250) refuse upstream tools.

## Verification

- `tests/unit/test_upstream_mcp_config.py` (grant matching, verified-only, store reload, bad section).
- `tests/unit/test_upstream_mcp_runtime.py` (a person's domains, a token's intersection, held domains).
- `tests/unit/test_scoped_tokens.py` (storage, create refusal, fingerprint, old tokens).
- `tests/unit/test_principal.py` and `tests/unit/web/test_server_org_mode.py` (the verified bit).
- `tests/unit/test_scoped_token_proposals.py` and `tests/integration/test_settings_scoped_token_form_browser.py`.
- `tests/integration/test_upstream_mcp_org.py`.

## Related

[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
[ADR 0236](0236-a-scoped-token-holds-its-own-rules.md),
[ADR 0272](0272-a-single-user-install-provisions-its-one-person-as-verified.md),
[ADR 0278](0278-upstream-mcp-servers-are-declared-connectors.md),
[ADR 0279](0279-privacyfence-sends-the-upstream-a-signed-caller-claim.md).
