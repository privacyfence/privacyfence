# ADR 0116: List and search tools page inside the client and keep their bare-list return shape

## Status

Accepted — 2026-09-30. Implemented: the `MAX_PAGES` loops in `src/privacyfence/*_client.py`.

## Context

Before this change, every connector list and search tool read one page from the provider and
returned it, whatever `max_results` asked for. Only the Slack channel, DM and group-chat listings
walked pages. A client asking for 200 items could silently receive 50, and the tool descriptions
said "only the first page" to explain it. ADR 0115 and PR 834 recorded this as a known limit while
completing every tool definition.

## Decision

Every client list or search method that returns one page follows the provider's pages itself. It
loops while fewer than `max_results` items are collected and the API returns a continuation token
or link, with a budget of `MAX_PAGES` requests per call (a module constant in each client that
pages). Each request asks for `min(remaining, api_page_max)` items where the API allows it. The
tool returns a bare list truncated to `max_results`, the same shape as before. A page that fails
raises the client's existing error, with no partial result. Tools take no `page_token` parameter.

## Alternatives considered

- **A `page_token` parameter and `{items, next_page_token}` on every list tool** — gives a client
  full traversal, but turns every bare-list result into a dict, touches every privacy-filter path
  (`apply_list` works on lists, and the Tasks notes filter would silently stop applying to a
  wrapped result) and every list test, for a benefit no client has asked for.
- **Leaving first-page-only results as documented limits** — keeps the surprise: `max_results`
  would keep meaning "at most, from the first page".

## Consequences

- `max_results` now means what it says, up to the tool's clamp and the page budget.
- A client cannot walk past `max_results`; to see more it must raise `max_results` within the clamp
  or narrow the query.
- The exception is Slack `slack_get_channel_history` and `slack_get_thread_replies`, which take a
  `cursor` and return `next_cursor`, because their results are ordered streams a client has to
  continue from where it stopped.
- Each paging client carries its own `MAX_PAGES`; the clients share no code and the APIs differ.

## Verification

The `MAX_PAGES` loops and their multi-page tests in `tests/unit/test_*_client.py`; the tool
descriptions are checked by `assert_tool_definitions_complete` (ADR 0115).

## Related

- ADR 0115 (tool definitions carry their guidance in prose)
- PR 834 (completed every tool definition and recorded these limits)
