# ADR 0325: An upstream can be the source of its own audiences

## Status

Accepted — 2026-11-18. Amends [ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md),
[ADR 0299](0299-group-membership-is-updated-through-a-scoped-token-tool.md),
[ADR 0319](0319-the-admin-page-shows-upstream-mcp-servers-read-only.md) and
[ADR 0323](0323-the-oidc-provider-sends-no-group-membership.md).
Relates to [ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md),
[ADR 0300](0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md),
[ADR 0310](0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md),
[ADR 0326](0326-a-service-claim-may-call-the-declared-audiences-tool.md).
Implemented: `src/privacyfence/upstream_audiences.py`, `upstream_mcp_config.py`, `upstream_mcp_runtime.py`,
`settings_window_html.py`, `scripts/upstream_mcp_reference_server.py`. Issue
[#1153](https://github.com/privacyfence/privacyfence/issues/1153); the record of the change is
[`docs/1153/plan.md`](../1153/plan.md).

## Context

Audiences, the labels an upstream uses for slices of its data, come from the group map in
`admin_config.json` and the group membership a job pushes through a scoped-token tool, where an addition
to any audience but `all` waits for an administrator (ADR 0298, ADR 0299). ADR 0323 kept both for scoped
tokens. An operator whose upstream already knows who may see what (a knowledge hub with its own teams) has
to copy that into the map and keep it in step by hand.

The issue first proposed a catalogue: the upstream lists its audience names, an administrator maps groups
to them and approves people. The owner decided otherwise: the upstream returns each audience's members and
is the authority for them (model `member`), no administrator approval per person or per mapping for an
audience from this source (approvals `none`), and a refresh at startup and then on an interval the
operator sets, five minutes by default.

## Decision

1. **A per-upstream opt-in.** The upstream declaration gets `audiences_tool` (a tool name, 1-48 letters,
   digits, underscores or hyphens; not also a key of the same upstream's `tools`; not with
   `identity_claim: off`) and `audiences_refresh_seconds` (a whole number from 30 to 3600, default 300;
   only with `audiences_tool`). An upstream without them behaves exactly as before. A change needs a
   restart, like the rest of the declaration.
2. **The member table.** PrivacyFence itself calls the tool with `{}` and a service claim (ADR 0326), at
   startup and then on the interval. The answer is `{"version": 1, "audiences": {<name>: [<address>, …]}}`,
   at most 200 audiences, 20000 addresses and 1 MB, with exactly those two keys. An address that
   `group_membership.lookup_email` rejects is dropped and counted, never a reason to refuse the answer;
   any other violation refuses the whole answer and the previous one stays. A person's audiences on that
   upstream are the audiences that list their verified email, and a scoped token still holds
   `creator's audiences now ∩ ticked`.
3. **No approvals, no cap.** The upstream may list anyone under any audience, `all` included. There is no
   `max_additions` check, no parking and no administrator step for this source. The group map in
   `admin_config.json` and the group-membership approvals are not used for that upstream: its map is
   ignored (and logged once at start), and `all_group_audiences` skips it, so the sync tool stores no
   member of a group mapped only for it and **Admin → Group membership** shows no pending row for it.
   Every table belongs to one upstream id, so no upstream can reach another upstream's audiences.
4. **Refresh, stale and fail closed.** A failed refresh keeps the last answer and retries with a growing
   delay (never longer than the interval). An answer older than three intervals gives nobody any
   audience until the next success, and before the first success nobody holds one. The tool list is still
   fetched once.
5. **Audit and visibility.** Two audit decisions, `upstream_audiences_applied` (first success, a table that
   changed, the first success after a failure) and `upstream_audiences_failed` (the first failure of a
   run), with counts and a failure kind and never an address; neither is an approval. The refresh writes
   no `upstream_forwarded` entry (its `jti` is logged at info). **Admin → Upstream MCP servers** and
   `privacyfence_status` show the source and its state (`waiting`, `ready`, `stale`), counts and the last
   refresh and error, never an address.
6. **Not in the declaration digest.** The source decides who holds an audience, not what a tool does, so no
   ADR 0300 approval depends on it.

## Alternatives considered

- **Push**: the upstream calls a PrivacyFence scoped-token tool, as the sync tool does (ADR 0299). The
  upstream would hold a PrivacyFence credential, a stolen one rewrites that upstream's audiences, and
  staleness could only be guessed from silence.
- **Delegate**: send the upstream only `email` and let it decide. A scoped token's claim carries no email
  (ADR 0310), so tokens could not be resolved or narrowed.
- **Catalogue with approvals** (the issue's first proposal): the owner chose `member` and `approvals: none`.
- **Reject the whole answer on one bad address**: one non-ASCII address would stop every refresh and,
  three intervals later, empty everyone's audiences.
- **Keep the last answer forever when the upstream is down**: a removal at the upstream would never reach
  PrivacyFence during an outage; the three-interval rule bounds the lag.
- **Pin the source in `declaration_digest`**: it changes who holds an audience, not what a tool does; an
  ADR 0300 approval stays valid.
- **Use both sources (union with the group map)**: two answers to one question, and the one that needs hand
  edits would silently widen.

## Consequences

- A removal at the upstream reaches PrivacyFence within one interval, and within three intervals plus the
  60 second call timeout while the upstream is down (3 hours 1 minute at 3600 seconds).
- An existing scoped token's ticked `upstream_audiences` for an upstream that switches source are read by
  name against the new source. There is no migration; this is documented.
- A compromised or careless upstream can widen access only to its own data, including to `all` on itself,
  with no administrator in the loop. That is the owner's decision.
- The audit log and the Admin page show that the source works, not who is in it.
- `upstream_audiences.py` is pure; the refresh task and its state live in `upstream_mcp_runtime.py`.

## Verification

- `tests/unit/test_upstream_audiences.py`: `TestParseResult` (shape, limits, dropped addresses, digest) and
  `TestSource` (`test_waiting_before_the_first_snapshot`, `test_stale_at_exactly_three_intervals_plus_one`,
  `test_older_seq_is_ignored`).
- `tests/unit/test_upstream_mcp_config.py`: the `audiences_tool` and `audiences_refresh_seconds` tests and
  `test_declaration_digest_ignores_the_audiences_source`.
- `tests/unit/test_upstream_mcp_runtime.py`: `TestAudiencesFromUpstream` and `TestAudiencesLoop`.
- `tests/unit/test_settings_window_html.py`: the `test_upstreams_audiences_*` tests.
- `tests/integration/test_upstream_mcp_org.py`:
  `test_audiences_from_the_upstream_let_a_person_read_that_audience`,
  `test_dropping_a_person_at_the_upstream_removes_access_after_a_refresh`,
  `test_the_audiences_refresh_writes_an_applied_audit_entry`.

## Related

[ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md),
[ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md),
[ADR 0299](0299-group-membership-is-updated-through-a-scoped-token-tool.md),
[ADR 0300](0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md),
[ADR 0310](0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md),
[ADR 0319](0319-the-admin-page-shows-upstream-mcp-servers-read-only.md),
[ADR 0323](0323-the-oidc-provider-sends-no-group-membership.md),
[ADR 0326](0326-a-service-claim-may-call-the-declared-audiences-tool.md).
