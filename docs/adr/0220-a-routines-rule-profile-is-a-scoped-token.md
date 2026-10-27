# ADR 0220: A routine's rule profile is a scoped token

## Status

Accepted — 2026-10-22. Implemented.
Relates to ADR 0164 and ADR 0015.
Amended by [ADR 0236](0236-a-scoped-token-holds-its-own-rules.md): a token holds its own copy of its rules.

## Context

A scheduled routine that runs unattended needs a fixed, named set of rules and tools, an expiry, a way
to revoke it, and an audit trail that names it. The request that led here asked for a "rule profile"
and a `profile` argument on `privacyfence_begin_unattended_session`.

The scoped token (ADR 0164) already is that: it carries named rules and tools, expires, is revoked
without touching other clients, is deny-only, works unattended, and is named in the audit log as
`scoped-token:<id>` with the token's label on every call. A call outside the token is denied as
`denied_scoped_token` even when an install-wide rule would cover it.

## Decision

- **The token is the profile.** No new object is added, and `privacyfence_begin_unattended_session`
  gets no `profile` argument. The routine presents the token as the bearer credential of its
  connection; a scoped token starts an unattended session without the administrator's
  `unattended_sessions.enabled` opt-in (ADR 0164).
- **The self-declared unattended flag stays advisory** (ADR 0015): nothing an agent sends selects its
  own rules.
- **The rollout is one token per routine.** A token keeps the operations its rules had when it was
  created, so a rule's verbs are edited first and the token is created afterwards.

## Alternatives considered

- **A `profile` argument on `privacyfence_begin_unattended_session`.** The agent would name its own
  rule set, which is the self-declaration ADR 0015 refuses to trust, and a second object would
  duplicate the token's expiry, revocation and audit identity.
- **A separate stored profile object.** It would need its own lifecycle, step-up and audit
  attribution for no property the token lacks.

## Consequences

- A routine reaches PrivacyFence with a credential only a person created and can revoke; the audit log
  attributes each call to it.
- A routine whose client can only sign in as the person (the claude.ai connector) cannot use a token;
  it needs a client that can send a bearer header.
- Adding a verb to a rule does not reach existing tokens.

## Verification

- `tests/unit/test_scoped_tokens.py`, `tests/unit/test_gate.py` (token scope, `rule_limits`,
  `denied_scoped_token`).
- `tests/unit/web/test_mcp_dispatch.py` (a scoped session needs no `unattended_sessions.enabled`).

## Related

- ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), ADR [0015](0015-unattended-session-flag-is-advisory-only.md), ADR [0219](0219-a-scoped-token-can-let-email-and-slack-reads-return-personal-data.md)
- [`scoped_tokens.py`](../../src/privacyfence/scoped_tokens.py), [`gate.py`](../../src/privacyfence/gate.py)
