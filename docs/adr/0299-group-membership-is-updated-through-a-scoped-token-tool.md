# ADR 0299: Group membership is updated through a scoped-token tool

## Status

Accepted — 2026-11-14. Relates to [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)
and [ADR 0250](0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md).
Implemented: `src/privacyfence/connectors/group_membership.py`, `group_membership.py`, `scoped_tokens.py`,
the Admin page's "Group membership" section. Issue [#1103](https://github.com/privacyfence/privacyfence/issues/1103).
Amended by [ADR 0311](0311-the-oidc-provider-sends-approved-group-membership-to-apps-that-opt-in.md), 2026-11-15.

## Context

Audiences come from Workspace groups (ADR 0298), so something outside PrivacyFence, an n8n job that reads the
Directory, has to tell PrivacyFence who is in which group. That job must be able to do it unattended, and
a stolen credential for it must not be able to give anyone access to restricted data on its own.

## Decision

The job calls the tool `privacyfence_update_group_membership` with `{"groups": {"<group>": ["<email>"]}}`
through the ordinary MCP endpoint, with a **scoped token**. The tool has gate `auto` and refuses any caller
that is not a scoped token whose tools include it (`group_membership_denied` in the audit log), so a
prompt-injected AI client cannot call it. Only an **administrator** can create a token that includes it;
`ScopedTokenStore.create` refuses anyone else, and an AI proposal for it is refused. A creator who later
loses the admin role keeps a working token until it expires or is revoked: there is no live admin lookup.

Each call is a snapshot. Groups no upstream's `groups` map names are ignored and never stored. **Removals
apply at once** and delete that person's approvals for the group. **Additions to `all` apply at once.**
**Additions to any other audience wait**: the person is parked until an administrator approves or declines
that (email, group, audience) in the Admin page's "Group membership" section; the pending list is derived,
never stored. Limits: at most 500 groups and 20000 member entries; the additions of a snapshot and of the
last 24 hours may not exceed `membership.max_additions` (default 20) or the whole snapshot is rejected and
nothing is applied (the first snapshot ever is exempt, for bootstrap); more than 500 pending approvals also
rejects it. The audit log records `group_membership_applied`, `_rejected`, `_denied`, `_approved` and
`_declined` with counts and group names, never addresses.

## Alternatives considered

- **An inbound admin-token route** — the control channel refuses an admin credential on purpose
  (`web/control_channel.py`); the tool fits the scoped-token model instead.
- **A popup gate for additions** — a scoped token is denied at a popup (ADR 0164), so the card could never be
  answered by the job; the wait is on the Admin page, answered by a person.
- **The 0600 digest file kept as the unused fallback** — the owner named a file the job writes as the fallback
  if the tool could not work; it fits, so the fallback was not built.
- **Apply additions at once** — a stolen sync token could then grant itself or anyone a restricted audience.
- **A rolling cap with a restore for removals** — the owner decided removals apply at once; mass removals by a
  stolen token are a denial of service, not a disclosure, and stay uncapped.

## Consequences

- A stolen sync token can remove people from groups and park additions, never grant a restricted audience;
  an addition that names `all` does apply.
- Membership addresses are not checked against the organization's allowed domains.
- A rejected snapshot is an audit entry, a log line and a row on the Admin page; there is no push to
  administrators.

## Verification

- `tests/unit/test_group_membership.py` (snapshot rules, caps, approvals, declines, departure).
- `tests/unit/connectors/test_group_membership_connector.py` (who may call the tool and the audit entries).
- `tests/unit/test_scoped_tokens.py` (only an administrator creates the token) and
  `tests/unit/test_scoped_token_proposals.py`.
- `tests/unit/web/test_routes_admin.py` (the Admin page's approve and decline routes).

## Related

[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
[ADR 0250](0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md),
[ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md).
