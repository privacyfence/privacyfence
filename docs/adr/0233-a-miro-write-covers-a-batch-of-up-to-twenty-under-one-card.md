# ADR 0233: A Miro write covers a batch of up to twenty items or connector lines under one approval card

## Status

Accepted — 2026-10-25. Implemented: `miro_create_items`, `miro_create_connectors` and
`miro_update_items`.

## Context

An assistant drawing a diagram adds many items at once. Miro's bulk create call takes 1 to 20 items
and is transactional, but it cannot create frames. Connector lines and item updates have no bulk
call. One approval card per item would make a twenty-sticky diagram twenty approvals; a card for
more than twenty items cannot be read.

## Decision

- **One card covers up to 20 items or connector lines.**
- **Sticky notes, cards, texts and shapes** are created with Miro's bulk call: all or none.
- **Frames** are not part of that call, so they are created first, one request each.
- **Updates and connector lines** go one request at a time after the approval. They stop at the
  first error, report what was already done, and never retry.

## Alternatives considered

- **One card per item** — a diagram of twenty sticky notes would be twenty approvals.
- **Batches beyond 20** — 20 is Miro's bulk limit, and a longer card cannot be read.
- **Retrying a failed update** — a repeat could apply a change twice on a board someone else is
  editing; the assistant is told what was done and decides.

## Consequences

- A batch of sticky notes, cards, texts and shapes cannot be half applied. A batch with updates,
  frames or connector lines can be, and the answer says how far it got.
- The card shows the whole batch, so a person approves exactly what is drawn.
- An assistant needing more than 20 makes several requests, each with its own card unless a
  `miro.board` rule covers it.

## Verification

- `tests/unit/connectors/test_miro_connector.py`: `TestCreateItems`, `TestCreateConnectors` and
  `TestUpdateItems` (the limit of 20, frames before the bulk call, stopping at the first error and
  naming what was done).
- `tests/unit/test_miro_client.py`: `TestCreateItems`, `TestCreateFrame`, `TestSetParent`.

## Related

- ADR [0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md),
  ADR [0231](0231-the-miro-connector-reads-adds-and-changes-and-never-deletes.md)
- [`connectors/miro.py`](../../src/privacyfence/connectors/miro.py),
  [`miro_client.py`](../../src/privacyfence/miro_client.py)
