# ADR 0319: The Admin page shows upstream MCP servers read-only

## Status

Accepted — 2026-11-16. Implemented: `src/privacyfence/upstream_mcp_runtime.py` (`admin_rows`), `src/privacyfence/web/routes_admin.py`, `src/privacyfence/settings_window_html.py`.
Amends [ADR 0258](0258-administration-is-a-separate-admin-page.md): the Admin page gains an Upstream MCP servers section.
Relates to [ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md).
Amended by [ADR 0325](0325-an-upstream-can-be-the-source-of-its-own-audiences.md), 2026-11-18.

## Context

An administrator could learn what upstream MCP servers were declared, and whether a server's tool list had
arrived, only from the `privacyfence_status` tool or the log. The declaration is an operator file read once at
startup ([ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md)), and the group-to-audience map is a
file edit, so the Admin page had nothing to show or change.

## Decision

1. **Read-only.** Admin gains an **Upstream MCP servers** tab between Agent runtimes and Group membership. It has
   no button and no input, and no new action or route.
2. **What it shows.** For each declared upstream: whether its tool list arrived (`ready` or
   `waiting_for_tool_list`), address, claim audience, identity-claim settings, who may call it, each declared
   tool in declaration order with its gate, verb, effect and whether it is exposed (`waiting`, `exposed` or
   `missing`), and the groups mapped to its audiences, sorted.
3. **The address is reduced** to its scheme and host (with port), followed by `/…` when the declared URL has a
   path or a query: either may carry a key.
4. **The upstream's own tool descriptions are not shown.** They are text from the upstream; the page shows what
   the operator declared.
5. **Which variable declared it, not where.** The page names `PRIVACYFENCE_UPSTREAM_MCP_FILE` or
   `PRIVACYFENCE_UPSTREAM_MCP`, never the file's path or the inline text.
6. **Shown even when nothing is declared.** The tab then says so and says how to declare one.

## Alternatives considered

- **Editing the declaration or the group map from the page.** The declaration is the operator's, read once at
  start; an admin browser session must not be able to redirect claims or widen audiences.
- **Hiding the tab when nothing is declared.** The empty state is how an administrator learns the feature exists.
- **Showing the URL's path or query.** Either may hold a key.

## Consequences

- Administrators see what is declared and whether each server answered without reading the log or calling the
  status tool.
- Nothing on the page can change the declaration or the group map.
- The page shows only the scheme and host of an address, so an operator who needs the path reads the declaration.

## Verification

- `tests/unit/test_upstream_mcp_runtime.py` (`TestAdminRows`), `tests/unit/web/test_routes_admin.py`
  (`TestUpstreamServers`, `test_state_shape`), `tests/unit/test_settings_window_html.py` (`TestOrgAdminSections`),
  `tests/integration/test_browser_smoke.py` (`_ORG_ADMIN_PAGE_SECTIONS`).

## Related

- Issue [#1145](https://github.com/privacyfence/privacyfence/issues/1145).
- [ADR 0258](0258-administration-is-a-separate-admin-page.md), [ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md), [ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md).
