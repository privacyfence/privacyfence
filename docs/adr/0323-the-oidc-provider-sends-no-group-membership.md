# ADR 0323: The OIDC provider sends no group membership

## Status

Accepted — 2026-11-17. Supersedes [ADR 0311](0311-the-oidc-provider-sends-approved-group-membership-to-apps-that-opt-in.md).
Relates to [ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md),
[ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md),
[ADR 0299](0299-group-membership-is-updated-through-a-scoped-token-tool.md),
[ADR 0310](0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md),
[ADR 0318](0318-pkce-can-be-turned-off-for-one-named-oidc-app.md).
Implemented: `src/privacyfence/oidc_provider.py`, `src/privacyfence/web/routes_oidc.py`,
`src/privacyfence/group_membership.py`, `src/privacyfence/upstream_mcp_runtime.py`,
`src/privacyfence/daemon_main.py`.
Issue [#1148](https://github.com/privacyfence/privacyfence/issues/1148).

## Context

- ADR 0311 let an OIDC app opt in to a `groups` claim so Forgejo could map Workspace groups to teams. The
  feature shipped only in the 8.0.0 pre-releases.
- In the knowledge hub's plan, Forgejo team membership comes from the hub's own team configuration, which sets
  the members of each team to exactly what it lists. A second writer, PrivacyFence's `groups` claim together with
  Forgejo's team-map removal, would fight it: each sign-in could undo what the hub configured.
- The group map to audiences (ADR 0298) and the group-membership approvals (ADR 0299) are still needed for
  scoped tokens, such as the ones delivered into n8n, which take their audiences only from that map.

## Decision

1. **The `groups` claim is removed.** `OidcClient` keeps `client_id`, `redirect_uris`, `secret` and
   `require_pkce`. `OidcClient.groups`, `.groups_allowed` and `.allows_group`, `GroupsUnavailableError`, the
   `member_groups` argument of `OidcProvider`, `MembershipResolver.groups_for` and its two implementations, and
   `upstream_mcp_runtime.approved_groups_for_email` are deleted. The token endpoint no longer answers
   `503 temporarily_unavailable`, and the daemon no longer checks a groups client at start.
2. **Discovery** lists `"scopes_supported": ["openid", "email", "profile"]` and
   `"claims_supported": ["sub", "email", "email_verified", "name"]`. The ID token and userinfo carry `sub`,
   `email`, `email_verified`, `name`, and `nonce` in the ID token when one was sent.
3. **A leftover setting refuses the start.** If `PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS` or
   `…_GROUPS_ALLOWED` has any non-empty value (`false` included), `clients_from_env` raises
   `ConfigurationError`:
   `PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS and PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS_ALLOWED were removed: the OIDC provider no longer sends a groups claim (ADR 0323). Turn off the app's group-to-team mapping first (in Forgejo, --group-team-map-removal), then remove both variables.`
   Unset or empty is fine.
4. **ADR 0298 and ADR 0299 stay**, for scoped tokens (n8n) only. Nobody has to keep the group map current for
   people any more.
5. **ADR 0310 and ADR 0318 are unchanged.** `email_for_visibility` and `group_membership.lookup_email` stay;
   Forgejo still signs in through PrivacyFence with `…_REQUIRE_PKCE=false`. `all_group_audiences` stays, because
   the Admin page and the sync tool use it.

## Alternatives considered

- **Keeping the opt-in code, unused.** Dead code on a trust boundary, and it invites the second writer back.
- **Ignoring the leftover variables.** An app that still has team-map removal on would then receive no groups and
  empty its teams without a word. Refusing the start fails closed.
- **Removing ADR 0298 and ADR 0299 as well.** Scoped tokens for n8n get their audiences only from the group
  map. A replacement, a `workflows:` section in the hub's config or tokens carrying their own audience list
  (which ADR 0298 rejected because such a list can outlive a revoked membership), needs its own design.

## Consequences

- An operator who used ADR 0311 must turn off Forgejo's group-team mapping (the group claim name, the group-team
  map and removal from synchronized teams) before removing the variables. Otherwise Forgejo may empty the mapped
  teams at the next sign-in.
- Forgejo team membership is no longer PrivacyFence's concern: manage it in Forgejo or with the tool that owns
  the teams.
- The group map and the Admin → Group membership approvals keep their purpose for scoped tokens and audiences.

## Verification

- `tests/unit/test_oidc_provider.py`: `TestRemovedGroupsSettings::test_a_groups_setting_refuses_the_start`,
  `TestRemovedGroupsSettings::test_an_empty_groups_setting_is_ignored`.
- `tests/unit/web/test_routes_oidc.py`: `test_discovery_document_exactly`, `test_full_flow_tokens_carry_no_groups`,
  `test_pkce_off_client_full_flow_without_pkce`.
- `tests/unit/test_daemon_main.py`: `test_an_oidc_client_with_pkce_off_logs_a_warning`.

## Related

[ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md),
[ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md),
[ADR 0299](0299-group-membership-is-updated-through-a-scoped-token-tool.md),
[ADR 0310](0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md),
[ADR 0311](0311-the-oidc-provider-sends-approved-group-membership-to-apps-that-opt-in.md),
[ADR 0318](0318-pkce-can-be-turned-off-for-one-named-oidc-app.md).
