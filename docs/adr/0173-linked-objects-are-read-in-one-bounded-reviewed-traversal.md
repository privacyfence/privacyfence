# ADR 0173: Linked objects are read in one bounded, reviewed traversal

## Status

Accepted — 2026-10-13. Implemented: `src/privacyfence/assets_client.py` (`related_objects`),
`src/privacyfence/connectors/assets.py` (`assets_get_related_objects`).

## Context

Assets answers questions by following references: a contract to its account, an asset to its
contract, and in reverse. An agent that needs the whole picture would otherwise make one call per
hop, each needing its own approval.

## Decision

- **`assets_get_related_objects` follows references outbound, inbound or both in one call.**
- **The walk is bounded**: depth at most 3 and at most 200 objects, both clamped. When the object
  budget is reached the result is marked `truncated`, edges to unseen objects are dropped, and
  the walk stops.
- **One review card shows the whole graph**: every object and every edge. The call is a SEARCH
  read, so a schema rule applies only when every returned object (the root included) is in an
  approved schema (ADR 0172).

## Alternatives considered

- **An unbounded walk** — one call could pull the whole CMDB past a single card.
- **One card per hop** — the agent would need several approvals for one question, and each card
  would show less context than the whole graph does.

## Consequences

- A user approves or declines exactly what the agent will see; a larger neighbourhood needs a
  second call from a narrower starting object.
- Inbound traversal costs an AQL query per batch of frontier ids.

## Verification

`tests/unit/test_assets_client.py::TestRelatedObjects`,
`tests/unit/connectors/test_assets_connector.py::TestGetRelatedObjects`.

## Related

[ADR 0172](0172-assets-schemas-types-and-attributes-are-auto-approved-and-object-reads-are-reviewed-per-schema.md),
[ADR 0171](0171-jira-assets-is-a-separate-read-only-connector-whose-atlassian-scopes-are-requested-only-when-switched-on.md).
