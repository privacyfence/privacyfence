# ADR 0328: The n8n node is published under the @privacyfence npm scope

## Status

Accepted — 2026-11-18.
Amends [ADR 0289](0289-the-n8n-node-ships-from-this-repository-and-is-published-with-provenance.md).

## Context

The n8n node was named as the unscoped package `n8n-nodes-privacyfence` and has never been published. The v8.0.0
publish run failed twice: first with E404, because no `NPM_TOKEN` was set and the package did not yet exist for
trusted publishing, then with E403, because the bootstrap token can write only to the `@privacyfence` scope.
Tracking issue: https://github.com/privacyfence/privacyfence/issues/1160.

## Decision

- The package name is `@privacyfence/n8n-nodes-privacyfence` and the node type is
  `@privacyfence/n8n-nodes-privacyfence.privacyFence`.
- The credential type `privacyFenceApi` is unchanged.
- The MCP `clientInfo.name` the node sends is the package name.
- The publish workflow reads the package name from `n8n/package.json`, so a later rename cannot break its
  "already published" check.

## Alternatives considered

- **Keep the unscoped name and issue an all-packages token.** The owner chose the scope, which also ties the
  package to the `privacyfence` npm organization; the bootstrap token already covers it.
- **Leave `clientInfo.name` unscoped.** It would no longer name the package that sends it.

## Consequences

- The first publish is the next stable tag after this merges.
- No version exists under the old name, so nothing is deprecated or redirected.
- `n8n/package-lock.json` keeps the old root name until it is refreshed, which `npm ci` tolerates.

## Verification

- `n8n/package.json`, `.github/workflows/publish-n8n-node.yml`, `src/privacyfence/agent_runtimes.py`.
- `tests/unit/test_agent_runtimes.py`, `tests/unit/test_runtime_credentials.py`, `n8n/test/mcpClient.test.mts`.

## Related

- Issue: [issue 1160](https://github.com/privacyfence/privacyfence/issues/1160).
- [ADR 0289](0289-the-n8n-node-ships-from-this-repository-and-is-published-with-provenance.md).
