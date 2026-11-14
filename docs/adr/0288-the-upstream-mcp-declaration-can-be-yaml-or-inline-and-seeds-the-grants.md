# ADR 0288: The upstream MCP declaration can be YAML or inline, and seeds the grants

## Status

Accepted — 2026-11-14. Implemented.
Amends [ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md) and [ADR 0287](0287-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md).

## Context

Issue [#1102](https://github.com/privacyfence/privacyfence/issues/1102): a Docker operator wants to declare upstream MCP
servers at install time, several at once, without writing JSON by hand or running a `docker exec` script to set who
sees which data domain. ADR 0285 allowed only a JSON file, and ADR 0287 kept grants only in `admin_config.json`.

## Decision

- **YAML is accepted for the file by its name** (`.yaml` or `.yml`); any other name stays JSON. **Inline text** in
  `PRIVACYFENCE_UPSTREAM_MCP` is always read as YAML, so one-line JSON works too.
- **A repeated key is refused** in YAML and in JSON, because a repeated tool name would silently keep the last gate.
  A merge-key override stays valid. No message quotes the text.
- **The file and the inline variable are never set together**; both refuse the start. Either is at most 256 KiB.
- **`grants` in an upstream seed `admin_config.json` once per upstream id** and never overwrite it. An id already
  present wins, even with empty grants; nothing is removed. After the first start the file owns the grants.

## Alternatives considered

- **A second `docker-compose` profile or reference service.** Not asked for.
- **Grants in the declaration authoritative at every start.** It would silently override an administrator's later
  edits, and the declaration and `admin_config.json` would disagree.
- **A YAML-only file.** It breaks existing JSON files.
- **Inline text in `privacyfence.env`.** That file is one `KEY=value` per line; a multi-line value is easy to break,
  so inline text goes in the Compose `environment:` block.

## Consequences

- An operator edits grants as a file after the first start; removing an upstream from the declaration leaves its grants.
- The tool-name collision message still starts with `PRIVACYFENCE_UPSTREAM_MCP_FILE` for an inline declaration.

## Verification

- `tests/unit/test_upstream_mcp_config.py`, `tests/unit/test_admin_config.py` (`TestSeedUpstreamGrants`),
  `tests/unit/test_daemon_main.py` (`-k upstream`), `tests/integration/test_org_docker_deploy_files.py`.

## Related

- Issue: [#1102](https://github.com/privacyfence/privacyfence/issues/1102); sources `src/privacyfence/upstream_mcp_config.py`,
  `src/privacyfence/admin_config.py`, `src/privacyfence/daemon_main.py`.
- [ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md),
  [ADR 0287](0287-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md).
