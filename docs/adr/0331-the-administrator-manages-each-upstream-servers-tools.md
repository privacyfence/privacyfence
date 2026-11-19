# ADR 0331: The administrator manages each upstream server's tools

## Status

Accepted — 2026-11-19. Implemented.
Supersedes [ADR 0319](0319-the-admin-page-shows-upstream-mcp-servers-read-only.md).
Amends [ADR 0300](0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md).
Relates to [ADR 0329](0329-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md),
[ADR 0330](0330-oauth-upstreams-discover-their-server-register-a-client-and-keep-tokens-per-person.md),
[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
[ADR 0258](0258-administration-is-a-separate-admin-page.md).
Issue [#1155](https://github.com/privacyfence/privacyfence/issues/1155); the record of the change is
[`docs/1155/plan.md`](../1155/plan.md).

## Context

Under the claim feature the operator declared each upstream tool and its gate in the file, and the Admin
page only showed them (ADR 0319). A hosted server lists its own tools, changes them without notice, and
describes them in its own words. PrivacyFence must decide which of those tools an AI client sees, which
ask the person first, and who may approve a scoped token's use of them, without trusting what the
server says about itself.

## Decision

1. **Refresh reads a server-wide list with the administrator's own connection.** An administrator who has
   connected presses **Refresh tools**; the runtime lists the tools with *that administrator's* token. The
   list is the server's, the same for everyone; it reads only the tool list and changes nothing at the
   vendor. A person whose account lacks a tool gets the vendor's error for it. An administrator who has
   not connected is told to Connect first.
2. **Annotations seed a default, never the final word.** A tool's class default gate is `review` only when
   the server lists `readOnlyHint=true` and not `destructiveHint=true`; every other tool, an absent hint
   included, is `popup`. The server's annotations are untrusted: a server can mark a write tool read-only.
   That residual is accepted because the administrator reviews every default before Save, the list
   shows each tool's verb, and a `review` gate still shows the person the result; the docs list it as
   a caution. The verb (`read`, `update`, `delete`) derives the same way and decides which scoped-token
   scopes cover the tool.
3. **New and changed tools are held until Save.** Until an administrator presses **Save**, AI clients keep
   seeing the previously saved list, whatever the server now lists. A tool whose digest changed is offered
   with its class default, not its saved gate. **Save** is bound to what was reviewed: it carries the
   pending list's `fetched_at` and is refused (409) when another administrator refreshed in between.
   Gate changes without a refresh are a separate **Save gates** action. A saved list made for another
   address is ignored, so a rule never outlives the server it was made for.
4. **`review`, `popup` or `auto`, and `auto` is a ceiling.** The gate a person meets is the configured gate
   when it is `review` or `popup`, else the class default. `auto` is never enforced as "no card": it
   permits a person to make an **Always allow** rule for that tool, and it is that rule that removes the
   card. Lowering a gate, or a changed digest, leaves stored rules in place and matching nothing.
5. **Live registration.** Saving registers the tools into the policy tables (gate, operation, verb,
   write effects, scope declarations, the `auto` set) under one lock, replacing what the upstream
   registered before, and `rebuild_registry` builds the new mapping before it deletes stale keys so no
   reader sees it empty. Then every person's connector host is evicted (`ConnectorRegistry.evict_all`) and AI
   clients are notified, so the new tools appear at once for people who are already connected (found by
   the integration test: without it a connected person's cached host kept the old list).
6. **The per-tool rule.** One predicate, `upstream.always_allow`, whose value is the exposed tool name and
   whose operations are that tool's operation; each `auto` tool has its own rule and its own row. A
   stored rule is kept as a custom row (so it survives a scope group disappearing), and the "fill missing
   operations" check skips it so it is never widened.
7. **Scoped tokens, and the digest.** A token that lists a gated upstream tool needs either the ADR 0300
   administrator tick, pinned to `declaration_digest` (URL, operation, verb, gate and tool digest, so a
   change to any of them voids the tick), or one of the token's own `upstream.always_allow` rules for that
   tool, which any creator may add. The token acts through its creator's connection
   ([ADR 0329](0329-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md)).
   This amends ADR 0300, which allowed only the tick.
8. **The Admin page and `privacyfence_status`.** The Admin state's `upstream_servers` rows (replacing
   ADR 0319's read-only listing) show per upstream the address, issuer host, registration, scopes, count of
   connected people (never who), saved tools and the pending list. The three actions are
   `refresh_upstream_tools`, `save_upstream_tools` and `set_upstream_tool_gates`; the last two are
   sensitive (admin passkey step-up). They return the HTTP status of `UpstreamAdminError`: 400
   (unknown upstream, bad gate, tool not saved), 409 (no pending list, stale `fetched_at`) and 502
   (the refresh failed at the vendor). Each writes an audit row (`upstream_tools_refreshed`,
   `upstream_tools_saved`, `upstream_tool_gates_changed`) with the upstream id and counts only.
   `privacyfence_status` carries `upstreams` rows from the runtime when it is configured (state
   `waiting_for_admin` or `ready`, `connected`, hints). The Connect rows are listed in sorted-id order,
   not declared order, because the runtime exposes the ids as a set.

## Alternatives considered

- **Take gates from the server's annotations as the final word.** The rejection in ADR 0285 stands:
  annotations only seed a default, and only `readOnlyHint=true` earns `review`.
- **Expose newly listed tools at once.** Owner decision: held until saved, so a server cannot change
  what an AI client can call by changing its listing (the rug-pull guard of ADR 0285, now "until an
  administrator saves" instead of "once at startup").
- **An unconditional `auto` path.** Owner decision: `auto` is a ceiling and a person's rule is what
  removes the card.
- **One Always allow rule per server.** Owner decision: per tool, so a rule for a read tool never covers
  a write tool added later.
- **Edit the declared tools in the operator file.** Dropped for the Admin page, so a restart is no longer
  needed to add a tool.
- **Refresh with a service credential.** The vendor offers none; the administrator's own connection is
  the only identity the vendor knows.

## Consequences

- Adding or changing a tool at the vendor needs an administrator's Refresh and Save, not a restart.
- Until the first Save a server exposes no tools.
- A server that mislabels a write tool as read-only gets a `review` default the administrator can raise.
- Rules are created person by person; an `auto` ceiling lets them, and does not make them.

## Verification

- `tests/unit/test_upstream_tools.py`: classification, verbs, digests, diff, unsupported reasons, the store.
- `tests/unit/test_upstream_mcp_registry.py`, `tests/unit/test_upstream_policy_scopes.py`: live
  registration, the ceiling, the per-tool rule and the digest.
- `tests/unit/test_upstream_mcp_runtime.py`: refresh, hold, `fetched_at` binding, the error statuses.
- `tests/unit/web/test_routes_admin.py`: the three actions and their audit rows.
- `tests/unit/web/test_mcp_dispatch.py`, `tests/unit/web/test_server.py`
  (`test_a_saved_upstream_list_evicts_every_connector_host_and_tells_clients`): status rows and eviction.
- `tests/integration/test_upstream_oauth_org.py`: refresh and hold, `auto` and Always allow, a changed
  tool, a scoped token's approval or rule.

## Related

Issue [#1155](https://github.com/privacyfence/privacyfence/issues/1155),
[ADR 0329](0329-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md),
[ADR 0330](0330-oauth-upstreams-discover-their-server-register-a-client-and-keep-tokens-per-person.md),
[ADR 0300](0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md),
[ADR 0319](0319-the-admin-page-shows-upstream-mcp-servers-read-only.md),
[ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md).
