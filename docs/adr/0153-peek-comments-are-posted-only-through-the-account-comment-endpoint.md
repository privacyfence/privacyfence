# ADR 0153: Peek comments are posted only through the account comment endpoint

## Status

Accepted — 2026-10-11. Implemented.

## Context

Peek's browser comment endpoint checks only the browser's access to the page, takes the author name
from the request body, and on a public page accepts anyone as "anonymous". It has no token path, so
a token sent there is ignored. A comment an approver approved "as me" would appear unsigned.

## Decision

`peek_add_comment` posts only to `POST /api/uploads/{slug}/account-comments`, a separate route that
requires the token, lets the page's owner or an admin comment, and records the token's account as
the author. On a Peek server without it (404 or 405) the tool fails with a message saying so; it
never falls back to the browser endpoint. Reading comments needs no new endpoint.

## Alternatives considered

- **Sending the token to the browser comment endpoint.** It ignores the token, so the comment would
  be unsigned.
- **Posting with a made-up author name.** The approval card could not say truthfully who the
  comment is from.

## Consequences

Commenting needs a Peek server that has the endpoint; every other tool works without it. PrivacyFence
cannot delete a comment it posted, and the approval card says so.

## Verification

- `tests/unit/test_peek_client.py`: `add_comment` calls only the account route and maps 404 and 405
  to the "no account comment endpoint" message.
- `tests/unit/connectors/test_peek_connector.py`: the popup card names the page and anchor, and the
  result carries the account's own author name.
- The live lifecycle check in `scripts/qa_fixture_recorder.py` posts a comment on a scratch page.

## Related

- [`peek_client.py`](../../src/privacyfence/peek_client.py), [`connectors/peek.py`](../../src/privacyfence/connectors/peek.py)
- ADR [0151](0151-peek-sign-in-uses-peeks-own-device-flow-and-keeps-the-token-per-principal.md), ADR [0152](0152-which-peek-server-is-used-and-a-token-stays-with-it.md)
