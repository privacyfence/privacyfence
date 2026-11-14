# ADR 0282: Runtime delivery is organization-mode only and starts only from an AI proposal

## Status

Accepted — 2026-11-13. Implemented.
Relates to [ADR 0250](0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md) and [ADR 0261](0261-local-mode-is-deprecated-in-7-and-removed-in-8.md).

## Context

Delivery needs an n8n address and key in the server environment, a daemon that runs the sweeper, and a person who
confirms. Local mode is deprecated in 7.x and removed in 8.x (ADR 0261), and the New token form has no AI client to
tell which workflow the token is for.

## Decision

- Delivery is available in organization mode, a single-user organization install included, and not in local mode.
  A proposal with `deliver_to` is refused in local mode, and the local create-from-proposal route refuses a delivery
  proposal.
- Only an AI proposal starts a delivery. The delivery target lives on the in-memory `TokenProposal`; the browser
  request can never set or change it, and the New token form has no delivery choice.
- Delivery needs `agent_runtimes.n8n.enabled`, the address and key in the environment, scoped tokens enabled and AI
  proposals allowed. Otherwise `deliver_to` is refused with a text that sends the person to Admin → Agent runtimes.
- The review page names the n8n host, the project (or the API key owner's personal space) and the reported workflow,
  and says anyone who can edit workflows there can use and read the token. The confirmation button reads "Create and
  deliver to n8n".

## Alternatives considered

- **Local mode too.** It would add a feature to a mode being removed, and its daemon has no place for the key.
- **The New token form as a second entry point.** The owner decided that only an AI proposal starts a delivery.

## Consequences

- A person who wants a token for n8n without an AI client copies it by hand, as before.
- Another runtime later needs a new ADR only if it changes these rules.

## Verification

- `tests/unit/web/test_routes_org_settings.py`: `TestDeliveryFromProposal` (a request body cannot change the target).
- `tests/unit/web/test_routes_settings.py` (the local route refuses a delivery proposal).
- `tests/unit/web/test_mcp_dispatch.py`: `TestProposeWithDelivery` (refused in local mode).
- `tests/unit/web/test_server.py` (no org directory is created for a local install).

## Related

- Issue: [issue 1090](https://github.com/privacyfence/privacyfence/issues/1090); sources `src/privacyfence/web/routes_settings.py`, `src/privacyfence/web/mcp_dispatch.py`.
- [ADR 0250](0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md),
  [ADR 0261](0261-local-mode-is-deprecated-in-7-and-removed-in-8.md), [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md).
