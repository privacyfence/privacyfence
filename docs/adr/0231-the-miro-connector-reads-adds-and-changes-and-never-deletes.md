# ADR 0231: The Miro connector reads, adds and changes, and never deletes

## Status

Accepted — 2026-10-25. Implemented: `connectors/miro.py` and `miro_client.py`.

## Context

Miro's REST API can delete boards and items, change a board's sharing policy and members, and,
for Enterprise organizations, export and audit whole organizations. A board delete or a frame
delete takes everything inside it, and an approval card could show only ids. Miro's public API
has no endpoint for comments. A parent change that Miro ignores (`{"parent": null}`) answers
HTTP 200 and changes nothing, which a live probe showed.

## Decision

- **The connector has ten tools:** it lists boards, tags, items and connector lines, reads boards
  and items, renames a board or changes its description, and adds and changes sticky notes, cards,
  texts, shapes, frames and connector lines. No tool calls any `DELETE`.
- **It leaves out** every Enterprise API (`/v2/orgs`), sharing and member changes, board `policy`,
  `teamId` and `projectId`, board create and copy, tag writes, groups, images, documents and
  embeds.
- **It cannot read comments,** because the public API has none.
- **Every move into or out of a frame is read back,** and the call fails when the parent is not
  what was asked for. The connector sends `{"parent": {"id": ""}}` to take an item out.

## Alternatives considered

- **Delete tools** — a board or frame delete removes its contents, and the card could show only
  ids.
- **Board `policy` changes** — they change who can see the board.
- **Enterprise export and audit APIs** — organization-wide compliance access, not one person's
  boards.
- **Trusting the 200 of a parent change** — Miro reports success for a change it ignored.

## Consequences

- A person who wants something removed does it in Miro; the assistant can only add and change.
- Tool annotations stay truthful: the three board and tag reads run without a card, the item and
  connector reads are reviewed, and every write needs approval
  ([ADR 0089](0089-tool-annotations-are-always-truthful.md)).
- Adding any of the left-out capabilities later is a new decision.

## Verification

- `tests/unit/connectors/test_miro_connector.py`: `test_no_tool_deletes_and_only_the_writes_write`
  and the tool definitions.
- `tests/unit/test_miro_client.py`: `test_move_out_sends_empty_id_never_null` and the read-back of
  a move.
- `tests/unit/connectors/test_readme_manifest_alignment.py`,
  `tests/unit/test_connector_tool_annotations.py`.

## Related

- ADR [0089](0089-tool-annotations-are-always-truthful.md),
  ADR [0145](0145-the-grist-connector-only-adds.md),
  ADR [0230](0230-miro-connects-only-by-oauth-with-an-expiring-token-app-in-the-bundle.md)
- [`connectors/miro.py`](../../src/privacyfence/connectors/miro.py),
  [`miro_client.py`](../../src/privacyfence/miro_client.py)
