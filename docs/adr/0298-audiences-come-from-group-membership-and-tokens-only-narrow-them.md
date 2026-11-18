# ADR 0298: Audiences come from group membership, and scoped tokens only narrow them

## Status

Accepted — 2026-11-14. Amends [ADR 0287](0287-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md)
and supersedes the seeding of grants from the operator file in
[ADR 0288](0288-the-upstream-mcp-declaration-can-be-yaml-or-inline-and-seeds-the-grants.md).
Implemented: `src/privacyfence/group_membership.py`, `upstream_mcp_config.py`, `upstream_mcp_runtime.py`,
`scoped_tokens.py`. Issue [#1103](https://github.com/privacyfence/privacyfence/issues/1103).
Amended by [ADR 0325](0325-an-upstream-can-be-the-source-of-its-own-audiences.md), 2026-11-18.

## Context

ADR 0287 decided who holds a data domain with grants in `admin_config.json`, each matching `*`, an email
domain or one address. An organization already keeps its people in Google Workspace groups, and the
upstream's own word "domain" means portfolio, product or technical area in a knowledge hub, so the
vocabulary collided. The grants had to be written and kept current by hand. The same trust rules stay:
an unverified address must not unlock data, and a scoped token can do less than its owner, never more.

## Decision

**Audiences, not domains.** What the claim and the upstream call a "domain" is now an *audience*, in the
claim, in `ScopedToken.upstream_audiences`, `held_audiences` and the New token form.

**Groups map to audiences.** `admin_config.json` holds `upstream_mcp.<id>.groups`, an object of group name
(lower case, email-shaped) to a list of audience names, at most 200 groups, re-read when the file changes. A
person's audiences are the union over the groups they belong to, from a **verified** email only. `all` is
the one unrestricted audience; every other one is restricted. `grants` are gone: the operator file refuses a
`grants` key at start with a message that points at the groups, and the seeding of grants from the declaration
(ADR 0288) no longer happens.

**Resolvers.** A resolver answers `audiences_for(email, group_audiences)`. `StaticFileResolver` reads a
JSON file and is for bootstrap and tests; `SnapshotResolver`, the default, reads the membership the sync
tool stored (ADR 0299). Default deny: an empty or unverified email, an unknown group and a group the map no
longer names give no audience.

**Approvals are bound to (email, group, audience).** An audience is granted only if the group's current map
names it, the person is currently a member, and either the audience is `all` or an administrator approved
that exact (email, group, audience). Approving `mgmt` for a person in one group is not an approval for any
other group, and an approval is forgotten when the person leaves the group.

**`sub` and tokens.** A person's claim has `sub` equal to their principal id and carries their email. A
scoped token's claim has `sub` `n8n-token:<token id>`, `approved_by` the creating person and no email: a
workflow does not act live for a person. A token holds `upstream_audiences`, a subset of what its creator
held when it was made, and each call uses the owner's audiences now intersected with that list, so a
creator who loses an audience takes it from the token on the next call. A token made with none sees none.

## Alternatives considered

- **Keep grants and add groups** — two sources of truth for one answer, and the one that needs hand edits
  would win.
- **Identity-provider group claims** — varies by provider and the sign-in claims are not what the owner chose;
  membership comes from the Workspace group list instead.
- **Trust any email address** — an unverified address would unlock another person's data.
- **A token that carries its own audience list** — it could outlive a revoked membership and could widen.
- **Name the audiences "domains" and keep the word** — collides with the hub's own meaning.

## Consequences

- An organization that sets `trust_unverified_email` for a domain accepts that its unverified addresses can
  hold the audiences of the groups they are in.
- A removal from a group stops working at the next call; an addition to a restricted audience waits for an
  administrator (ADR 0299).
- A development build's scoped token loses its domains; there is no migration, nothing was released.
- The Admin page has a control for approvals, not for the group map, which is still a file edit.

## Verification

- `tests/unit/test_group_membership.py` (resolvers, default deny, approvals, the map).
- `tests/unit/test_upstream_mcp_config.py` and `tests/unit/test_upstream_mcp_runtime.py` (the map, a person's
  audiences, a token's intersection, held audiences).
- `tests/unit/test_scoped_tokens.py` and `tests/integration/test_settings_scoped_token_form_browser.py`.
- `tests/integration/test_upstream_mcp_org.py`.

## Related

[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
[ADR 0236](0236-a-scoped-token-holds-its-own-rules.md),
[ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md),
[ADR 0287](0287-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md),
[ADR 0297](0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md),
[ADR 0299](0299-group-membership-is-updated-through-a-scoped-token-tool.md).
