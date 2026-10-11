# ADR 0152: Which Peek server is used, and a Peek token stays with the server it was issued for

## Status

Accepted — 2026-10-11. Implemented.

## Context

Peek is self-hosted, so there is no fixed address. Whoever chooses the address chooses where page
content, comments and the token are sent. In organization mode that must not be a person's free
choice.

## Decision

- In local mode without a bundle entry, the person chooses the server. A bundle's `peek.base_url`
  (`--peek-base-url`) pins it; organization mode offers Peek only when the bundle has one.
- A token is used only with the server it was saved with. If the pinned server differs, the
  connector refuses and asks the person to connect again.
- Only `https` addresses are accepted, plus `http` to `localhost`, `127.0.0.1` and `::1`; no user
  name, password, query or fragment. Redirects are never followed. The sign-in address Peek answers
  with must be on the same server and path prefix.
- Every Peek approval card starts with the server's host.
- Starting a sign-in stays a non-sensitive Settings action, like every connector's; enabling the
  connector stays sensitive (ADR [0070](0070-enabling-a-connector-is-sensitive-and-disabling-is-not.md)).

## Alternatives considered

- **A free server address in organization mode.** A person could send organization data to any
  server.
- **Making starting a Peek sign-in a step-up action because it chooses the destination.** The
  settings API is not reachable with an AI client's MCP token (ADR
  [0061](0061-the-mcp-token-and-the-browser-session-are-audience-separated.md)), and every card
  names the server on every call; the Grist connector made the same choice.

## Consequences

Moving a Peek server to a new address means everyone connects again. A Peek `--base-url` that
differs from what was typed (alias, port, `http` behind a TLS proxy) fails with a clear message
instead of sending a token somewhere unexpected.

## Verification

- `tests/unit/test_peek_client.py`: address normalization, redirects refused, a sign-in address on
  another server refused, and the pinned-server mismatch.
- `tests/unit/connectors/test_peek_connector.py`: every card's preview starts with the server.
- `tests/unit/test_build_org_bundle.py`, `tests/unit/web/test_routes_connect.py` and
  `tests/unit/test_settings_controller.py`: the pin, and Peek offered in organization mode only with it.

## Related

- [`peek_client.py`](../../src/privacyfence/peek_client.py), [`scripts/build_org_bundle.py`](../../scripts/build_org_bundle.py)
- ADR [0151](0151-peek-sign-in-uses-peeks-own-device-flow-and-keeps-the-token-per-principal.md), ADR [0143](0143-the-bundle-pins-the-grist-server-and-a-credential-stays-with-its-server.md)
