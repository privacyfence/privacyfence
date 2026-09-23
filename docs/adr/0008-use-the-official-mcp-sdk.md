# ADR 0008: use the official `mcp` SDK for Streamable HTTP, not a hand-rolled transport

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-08-28 in
`docs/https-connector-refactor-plan.md` §8.2 and §15 D2, deleted in `96cd5af4`). Implemented: the
SDK became a runtime dependency at phase P2 and still is.

## Context

Moving Claude's connection to PrivacyFence from the Node bridge's IPC socket to an HTTP `/mcp`
endpoint meant the daemon had to serve MCP's Streamable HTTP transport itself, and in org mode also
OAuth 2.1 protected-resource metadata. At the time, `mcp>=1.28,<2.0` was already in the repo as a
test-only dependency, used by the bridge/daemon contract test.

The repo has a standing rule to prefer the standard library over new dependencies
(`CONTRIBUTING.md`, restated in `docs/coding-and-testing-guidelines.md`). Using the SDK breaks it,
so the plan required the justification to be written down.

The facts that mattered, as the plan stated them: Streamable HTTP plus OAuth 2.1 metadata is a
security-critical specification that is still moving, and the acceptance criterion is conformance
with Claude's client, not with PrivacyFence's reading of the spec.

## Decision

The official MCP Python SDK (`mcp`) is a runtime dependency and serves `/mcp`, hosted on an ASGI
stack of `starlette` + `uvicorn`. This is a deliberate, named exception to "stdlib first".

A follow-on decision (D10, 2026-08-28) brought `starlette` + `uvicorn` in one phase earlier, at P1,
for the web approval surface, so that a stdlib asyncio server would not be written at P1 and thrown
away at P2. The plan described that as front-loading D2's single deviation, not adding a second one.

## Alternatives considered

- **Hand-rolled Streamable HTTP on `asyncio`**, in the style of the then-existing `ipc_server.py`.
  The plan called this genuinely feasible (the JSON-RPC framing was already hand-rolled) and noted
  its advantages: a smaller dependency footprint and a smaller PyInstaller bundle. It was rejected
  because hand-rolling means owning spec drift indefinitely, and reaching full test coverage on a
  hand-written HTTP/SSE/OAuth stack is a large test surface that buys nothing a maintained SDK does
  not already provide. In the plan's words, spec-tracking cost outweighs dependency minimalism here.

## Consequences

- PrivacyFence tracks the SDK's releases, including breaking ones. The 1.x to 2.x server API change
  was absorbed as a migration (issue https://github.com/privacyfence/privacyfence/issues/250); the
  pin is now a v2-only floor (`mcp>=2.2,<3.0.0`) because 2.0's server API is not additive over 1.x.
- The same reasoning ("security-critical, spec-governed, don't hand-roll it") was later cited by
  name for other dependencies: the SDK's own OAuth authorization-server routes (ADR 0010), `PyJWT`
  for ID-token verification, and `webauthn` for step-up. Each is recorded in `pyproject.toml`'s
  comments as an application of D2 rather than a new exception.
- `starlette` is on the security-relevant path: its Host/URL handling feeds the `Origin` checks in
  `web/session_auth.py` and `web/org_session.py`, which is why its floor was raised for CVEs
  (SEC-19, see the comment above the `starlette` pin).
- The stdlib-first rule still governs everything else; this ADR does not widen it.

## Verification

- `pyproject.toml` `[project] dependencies`: `starlette`, `uvicorn`, and `mcp`, with a comment
  naming D2 as the reason `mcp` became a runtime dependency.
- `src/privacyfence/web/routes_mcp.py` hosts `/mcp` on the SDK (and, in org mode, mounts
  `mcp.server.auth.routes.create_auth_routes` and `create_protected_resource_routes`).
- `src/privacyfence/web/server.py` starts the ASGI app under `uvicorn`.
- `tests/integration/test_mcp_daemon_contract.py`, `tests/unit/web/test_routes_mcp.py` and
  `tests/unit/web/test_org_mcp_e2e.py` drive `/mcp` with the official `mcp` client.
- `docs/TECHNICAL_REFERENCE.md` states that `/mcp` uses the official MCP Python SDK.

## Related

- `git show 96cd5af4^:docs/https-connector-refactor-plan.md` §8.2, §15 D2 and D10.
- Commit `88fcfdaf` (D1–D7 recorded as decisions), commit `866a0827` (D10).
- Issue https://github.com/privacyfence/privacyfence/issues/250 (migration to the SDK's 2.x API).
- ADR 0010 (org mode's authorization server, built on the SDK's auth routes).
- ADR 0006 (quotes an older `mcp` range, `>=1.28,<3.0`; `pyproject.toml` is authoritative).
