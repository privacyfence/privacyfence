# ADR 0311: The OIDC provider sends approved group membership to apps that opt in

## Status

Accepted — 2026-11-15. Implemented: `src/privacyfence/oidc_provider.py`, `src/privacyfence/web/routes_oidc.py`,
`src/privacyfence/group_membership.py` (`groups_for`), `src/privacyfence/upstream_mcp_runtime.py`
(`approved_groups_for_email`), `src/privacyfence/daemon_main.py`.
Amends [ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md) and
[ADR 0299](0299-group-membership-is-updated-through-a-scoped-token-tool.md).
Relates to [ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md).
Issue [#1133](https://github.com/privacyfence/privacyfence/issues/1133).

## Context

Forgejo team membership has to follow the Workspace groups. PrivacyFence already holds that membership for
audiences (ADR 0298, ADR 0299), with an administrator's approval before an addition to a restricted audience
takes effect. ADR 0273's provider published only `sub`, `email`, `email_verified` and `name`, so an app had no
way to read the groups. Forgejo can map a `groups` claim to teams and remove people from teams that the claim
no longer names.

## Decision

1. **PrivacyFence is the source of team membership.** One membership, one approval step and one audit trail;
   no second sync job.
2. **Opt-in per app, with an allowlist.** `PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS=true` (exact `true` or
   `false`, default `false`) adds a `groups` list to that app's ID token and userinfo. `…_GROUPS_ALLOWED` is
   a comma-separated list of exact group names or prefixes ending in `*` (at most 50, lower-cased); empty
   sends every mapped group, and the daemon warns at start. `…_GROUPS_ALLOWED` without `…_GROUPS=true` is a
   configuration error. An app that does not opt in gets tokens unchanged.
3. **Only approved membership.** A group counts when it is in some upstream's group map and every restricted
   audience it maps to is approved for the person (an audience `all` or no audience counts at once). A group
   in no map never counts. `approved_groups_for_email` merges every upstream's map, so a group mapped to
   restricted audiences in two upstreams counts only when all of them are approved. An unverified address or
   one `lookup_email` (ADR 0310) refuses gets no groups. A pending addition never appears; a removal applies
   at the person's next sign-in. A person with no approved membership gets `groups: []`.
4. **Sent whatever the scope.** `groups` is sent whether or not the app requested a `groups` scope; the
   discovery document lists the scope and claim for every app. An app that omits the scope would otherwise
   receive nothing, and `--group-team-map-removal` would remove everyone from their teams. The opt-in is the
   gate.
5. **Fail closed.** A client with `…_GROUPS=true` while no upstream is declared refuses the start (the
   groups would always be empty). A membership lookup that raises is logged without an address and answers
   the token request `503 temporarily_unavailable`, so the sign-in fails and is retried; `[]` is never sent in
   its place.
6. **New consequence for ADR 0299.** An addition to a group mapped to `[]` or to `all` applies at once and
   reaches the app at the next sign-in, so a stolen sync token can grant such a team, up to
   `membership.max_additions` per 24 hours. A restricted audience used only for the team (by convention
   `forgejo-<team>`) keeps the team behind an approval; an approval of that audience also grants the team,
   and the same audience name in a hub claim's `audiences` would unlock hub data too. Mapping a group to
   `all` also grants the hub's unrestricted audience. Remapping a group from a restricted audience to `[]`
   makes every stored member count at once.
7. **Limits.** Groups are read at sign-in only, so a removal lands at the next sign-in and the app's session
   lifetime bounds the delay. A `static` membership file has no approvals, so every listed member's group is
   sent.

## Alternatives considered

- **A second sync job for Forgejo teams** — it would add members without the approval step ADR 0299 requires.
- **Identity-provider group claims** — ADR 0298 already rejected them.
- **Sending groups only when the `groups` scope is requested** — an app that omits the scope would send
  nothing and, with team-map removal, remove everyone from their teams; the per-app opt-in is the gate.
- **Pending additions in the claim, or all stored groups** — would grant access before approval.
- **A separate OIDC-only group approval, or refusing groups with no restricted audience** — not chosen: the
  existing approval of the audience is the one step, and the operator decides which groups are mapped.

## Consequences

- An operator can sign Forgejo in with approved groups, and team membership follows Admin → Group membership.
- A team behind `[]` or `all` is only as safe as the sync token (ADR 0299); the docs tell the operator to use
  a restricted `forgejo-<team>` audience used for nothing else.
- A removal is not immediate: it waits for the person's next sign-in and the app's session lifetime.
- A groups app on a server with no upstream declared cannot start, and a broken lookup fails the sign-in
  rather than silently emptying teams.
- Discovery now lists the `groups` scope and claim for every app, opted in or not.

## Verification

- `tests/unit/test_oidc_provider.py`: `TestClientGroupsEnv` (`test_groups_default_off`,
  `test_groups_true_parses_with_entries`, `test_groups_must_be_true_or_false`,
  `test_allowed_without_groups_is_refused`, `test_a_bad_entry_is_refused`,
  `test_more_than_fifty_entries_are_refused`), `TestAllowsGroup`, and `TestClientGroups`
  (`test_an_opted_in_client_gets_groups_in_both_places`, `test_a_client_not_opted_in_has_no_groups_key`,
  `test_the_allowlist_filters`, `test_the_verified_flag_is_passed_on`,
  `test_no_callable_gives_an_empty_list`, `test_a_person_with_no_membership_gets_an_empty_list`,
  `test_a_failing_lookup_raises_and_logs_no_address`).
- `tests/unit/web/test_routes_oidc.py`: `test_discovery_document_exactly`,
  `test_full_flow_userinfo_carries_groups_for_an_opted_in_client`,
  `test_token_answers_503_when_the_groups_lookup_fails`.
- `tests/unit/test_group_membership.py::TestResolvers`: `test_groups_for_returns_an_all_only_group_at_once`,
  `test_groups_for_waits_for_the_approval_of_a_restricted_audience`,
  `test_groups_for_needs_every_restricted_audience_approved`,
  `test_groups_for_returns_a_group_mapped_to_no_audience`,
  `test_groups_for_drops_a_group_the_map_no_longer_names`, `test_groups_for_applies_a_removal_at_once`,
  `test_groups_for_empty_email_is_empty`, `test_static_groups_for_lists_member_groups_in_the_map`,
  `test_static_groups_for_ignores_a_group_not_in_the_map`.
- `tests/unit/test_upstream_mcp_runtime.py::TestApprovedGroups`: `test_a_verified_member_gets_the_sorted_groups`,
  `test_look_alike_addresses_give_nothing`,
  `test_a_group_two_upstreams_map_counts_once_every_restricted_audience_is_approved`.
- `tests/unit/test_daemon_main.py`: `test_oidc_provider_gets_the_approved_groups_lookup`,
  `test_groups_client_without_an_upstream_refuses_the_start`,
  `test_groups_client_without_an_allowlist_warns`.

## Related

[ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md),
[ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md),
[ADR 0299](0299-group-membership-is-updated-through-a-scoped-token-tool.md),
[ADR 0310](0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md).
