# ADR 0154: Peek visit stats carry counts, visitor names and times, never IP addresses

## Status

Accepted — 2026-10-11. Implemented.

## Context

Peek's stats answer lists each recent visit with the visitor's name, IP address, user agent and
time. An AI client summarising who opened a page has no use for the address or the browser, and
both are personal data about people who never took part in the conversation.

## Decision

`peek_get_stats` returns the total and unique visit counts and, for recent visits, the name the
visitor gave and the time. The client drops `ip` and `user_agent` as it parses Peek's answer, so no
later layer can carry them. The tool is behind review, and the card says what is returned.

## Alternatives considered

- **Returning them after review.** Personal data the assistant does not need, and the card could not
  make that visible enough to approve knowingly.

## Consequences

Questions such as "which office opened this page" cannot be answered through PrivacyFence. Anyone who
needs them reads them in Peek.

## Verification

- `tests/unit/test_peek_client.py`: `test_ip_and_user_agent_dropped`.
- `tests/unit/connectors/test_peek_connector.py`: the stats result and card carry names and times only.

## Related

- [`peek_client.py`](../../src/privacyfence/peek_client.py), [`connectors/peek.py`](../../src/privacyfence/connectors/peek.py)
- ADR [0151](0151-peek-sign-in-uses-peeks-own-device-flow-and-keeps-the-token-per-principal.md)
