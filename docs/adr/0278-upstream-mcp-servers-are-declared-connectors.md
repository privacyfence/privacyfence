# ADR 0278: Upstream MCP servers are declared connectors

## Status

Accepted — 2026-11-14. Implemented: `src/privacyfence/upstream_mcp_config.py`, `upstream_mcp_registry.py`,
`upstream_mcp_runtime.py`, `upstream_mcp_client.py`, `connectors/upstream_mcp.py`.

## Context

Issue [#1091](https://github.com/privacyfence/privacyfence/issues/1091): an organization wants its own
MCP server (a knowledge hub, for example) reachable by AI clients through PrivacyFence, with the same
tool-level policy as a built-in connector (cards, scoped tokens, unattended denial, audit log). The
policy tables (`TOOL_TO_GATE`, `TOOL_TO_VERB`, `EFFECT_BY_TOOL` and the layout table) are keyed by tool
name and filled at import, and gating is decided by the connector, not by the upstream. An upstream
cannot be trusted to say which of its tools write.

## Decision

An upstream is **declared** by the operator in a JSON file named by `PRIVACYFENCE_UPSTREAM_MCP_FILE`,
read once at startup, organization servers only (any other mode refuses the start). Per tool the file
gives a `gate` (`auto`, `review`, `popup`) and a policy `verb`. The gate follows the verb: a read verb
(`read`, `download`, `search`) takes `auto` or `review`; every other verb takes `popup` and must carry an
`effect` sentence, so an ungated write cannot be declared. A writing tool declared with a read verb is the
operator's error and is documented as such.

The AI client sees `<id>_<tool>`. The id may not collide with a built-in connector name. At startup the
declared tools are registered into the existing policy tables and the two derived values
(`TOOL_REGISTRY`, `ALL_OPERATIONS`) are rebuilt; a name already taken is a start-refusing error. The tool
list is fetched from the upstream once at startup (retried with backoff, never refreshed), and only
declared tools the upstream lists, with a portable schema, are exposed; the schema is passed through
verbatim plus a required `reason`. No scope, rule or "Always allow" offer exists for upstream tools yet.

The upstream's own error text reaches the AI client, cut to 500 characters, as its results do; the two
error types are named `RuntimeError` subclasses, which `safe_errors` already passes.

A connector with no external provider has no live fixture: layer 5 of the testing policy does not
apply. A reference upstream server (`scripts/upstream_mcp_reference_server.py`) stands in, driven by unit
and integration tests.

## Alternatives considered

- **Take gates from the upstream's tool annotations** — an upstream could mark a write as read-only, and
  an operator could not see what PrivacyFence would do.
- **Look policy up through functions instead of module-level dicts** — a large diff across about fifteen
  modules for tools that change only at restart.
- **Refresh the tool list periodically** — a description changing mid-session is a rug-pull vector; a
  restart is the operator's explicit act.
- **Allow desktop mode** — the claim needs an organization identity and a public signing key address.

## Consequences

- Changing an upstream's tools needs a restart; `privacyfence_status` shows `waiting_for_tool_list`
  until the first list arrives and names declared tools that are missing.
- Rules for upstream tools (and scoped-token rules for `review` and `popup` tools) are a later change.
- A writing tool declared with a read verb skips the approval card; the doc warns about it.
- Lookups keyed by connector name over the live set skip upstream ids.

## Verification

- `tests/unit/test_upstream_mcp_config.py` (file rules, id collisions, verb/gate rule).
- `tests/unit/test_upstream_mcp_registry.py` (registration, collisions, `unregister_all`).
- `tests/unit/test_upstream_mcp_client.py` and `tests/unit/connectors/test_upstream_mcp_connector.py`
  (catalog, schema pass-through, gates, error text).
- `tests/unit/test_upstream_mcp_runtime.py` (startup fetch, status).
- `tests/unit/web/test_tool_schema_portability.py` and `tests/unit/test_systemic_gate_invariants.py`.
- `tests/integration/test_upstream_mcp_org.py` (a real organization server and the reference upstream).

## Related

[ADR 0077](0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md),
[ADR 0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md),
[ADR 0196](0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md),
[ADR 0279](0279-privacyfence-sends-the-upstream-a-signed-caller-claim.md),
[ADR 0280](0280-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md).
