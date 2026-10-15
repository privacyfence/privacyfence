# ADR 0164: scoped tokens are named, expiring, rule- and tool-scoped, deny-only credentials

## Status

Accepted — 2026-10-11. Implemented.
Amends [ADR 0037](0037-a-local-override-is-a-relabel-and-never-attests.md), [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md), [ADR 0008](0008-one-principal-per-os-user.md) and [ADR 0015](0015-unattended-session-flag-is-advisory-only.md).
Amended by ADR 0173.

## Context

Every AI client of one person used the same credential: the local MCP token (one per OS account,
no expiry, ADR 0008, ADR 0104) or, in organization mode, an OAuth sign-in. Whatever held it could
use every auto-accept rule the person has and see every tool. A scheduled job, such as a
`privacyfence-sync` recipe, needs much less, and nothing could tell its calls from the person's
own in the audit log.

## Decision

A person can create **scoped tokens** in Settings, in both modes.

- A token has a label (an exact, case-sensitive duplicate is refused), an expiry (90 days by
  default, at most 365 locally and at most the administrator's cap in organization mode) and a
  scope: a set of auto-accept rule ids and a set of tools. At most 50 per person.
- The secret is `pfst_` plus 32 random bytes from `secrets`. Only its SHA-256 is stored, in
  `scoped_tokens.json` in the person's `authority/` folder. It is accepted only on `/mcp`, never
  on the bearer file-bridge routes.
- A client presenting it sees only the tools in its scope and a few meta tools (status, check_policy, upload slots and the two unattended-session
  tools). `privacyfence_list_policy` (it would show the person's other rules),
  `privacyfence_await_approval` and `privacyfence_propose_policy_change` are never offered. A call auto-accepts only through one of the token's rules; the temporary
  "Allow once" window is not consulted. Anything else is denied at once as
  `denied_scoped_token`: a scoped token never gets an approval card and never leaves a pending
  approval, like an unattended session.
- Because a scoped call is deny-only already, `privacyfence_begin_unattended_session` succeeds for
  it without the administrator's `unattended_sessions.enabled` opt-in, and cannot be ended into a
  more permissive state.
- The audit log names the token: the agent is `scoped-token:<id>` with the label as its name,
  source `override`. A credential the person created is attested, so this outranks an
  organization pin for that call.
- A token is revoked on its own, or all at once, without affecting the person's other clients. A
  rule removed or changed in Settings simply stops matching. A missing record is never widened to
  the person's full access.

## Alternatives considered

- **A narrowed OAuth grant in organization mode** — the SDK's dynamic client registration flow has
  no scope negotiation PrivacyFence uses, the refresh chain ends after 30 days (ADR 0072), which
  cannot carry a 90-day job, and every run would depend on a refresh token kept by the client.
- **Showing the local token in the response, behind a passkey** — ADR 0104 rejected it for the main
  token: any process running as the user can already reach the companion, so the passkey adds
  nothing and the secret would sit in an HTTP response. Local scoped tokens use the companion the
  same way.
- **Storing or encrypting the secret so it can be shown again** — a hash verifies a bearer token;
  anyone who loses it creates a new one.
- **Scoping by tools only, or by rules only** — rules decide what auto-accepts, tools decide what
  the client sees and can call, including `auto` tools no rule governs. Both are needed to make a
  job's reach exactly its recipe's.
- **Letting a scoped token ask for approval** — a scheduled job has nobody to answer, and a card
  from a narrow credential would invite approving things outside its purpose.
- **A new audit field for the token** — the agent fields already say who made a call.
- **Accepting scoped tokens on the bearer file-bridge routes** — only the `.mcpb` shim uses them,
  and the shim always holds the main token.

## Consequences

- A scheduled job can hold a credential that reaches exactly its recipe's tools and rules, expires
  on its own, and is revoked alone.
- Local mode now has an attested agent source, which ADR 0037 reserved `override` for.
- A token is a long-lived bearer secret: whoever copies it holds its scope until it expires or is
  revoked. Settings flags tokens with 14 days or fewer left, and `privacyfence_status` reports the
  expiry to the client.
- Rules are referenced by content-derived id, so editing a rule silently narrows tokens that used it.

## Verification

`tests/unit/test_scoped_tokens.py` (the store), `TestScopedTokenVerifier`, `TestScopeFromAccessToken`
and `TestScopedTokenPrincipals` in `tests/unit/web/test_mcp_auth.py`, `TestScopedTokens` in
`tests/unit/web/test_routes_mcp.py`, `TestScopedTokenIdentity` in
`tests/unit/web/test_routes_mcp_agent_identity.py`, `TestScopedTokenGate` in
`tests/unit/test_gate.py`, `TestScopedTokenRoutes` in `tests/unit/web/test_routes_settings.py` and
`TestScopedTokensSection` in `tests/unit/test_settings_window_html.py`.

## Related

[ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md),
[ADR 0008](0008-one-principal-per-os-user.md), [ADR 0015](0015-unattended-session-flag-is-advisory-only.md),
[ADR 0037](0037-a-local-override-is-a-relabel-and-never-attests.md),
[ADR 0061](0061-the-mcp-token-and-the-browser-session-are-audience-separated.md),
[ADR 0104](0104-the-companion-copies-the-ai-client-token.md),
[ADR 0072](0072-org-mode-persists-only-sealed-refresh-tokens.md),
[ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md).
