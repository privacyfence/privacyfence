# ADR 0239: The Peek connector uses Peek's own visibilities, and new pages are restricted

## Status

Accepted — 2026-10-27. Implemented.
Relates to 0158.

## Context

Peek changed who can open a page. Its migrations 14 and 15 rewrote `private` pages to `signed-in`
and password pages to `restricted`, and Peek now answers HTTP 400 to any request that carries a
page password. Peek stores and returns `public`, `signed-in` and `restricted`; it still accepts
`private` as input and maps it to `signed-in`. A page uploaded without a visibility is
`restricted`, which is Peek's own default. The connector still spoke `public`, `password` and
`private`, so `peek_upload_page` and `peek_set_visibility` with `password` always failed, and
`peek_list_pages` showed a value (`signed-in`) that the tools refused as input. The access model
is described in Peek's own docs:
<https://github.com/andras-tkcs/peek/blob/main/docs/src/content/docs/auth-access.md>.

## Decision

1. The connector uses Peek's three values, `public`, `signed-in` and `restricted`
   (`peek_client.VISIBILITIES`). `peek_client.normalize_visibility` strips the input and maps
   `private` to `signed-in`.
2. A page password is refused with a message that says to restrict the page and grant access
   (`peek_grant_access`). The `password` argument is gone from both tool specs, but the handlers
   keep a hidden keyword, so a client that still sends it gets that message instead of a
   `TypeError`.
3. `peek_upload_page` without a slug and without a visibility publishes a `restricted` page. A new
   version of an existing page keeps the page's visibility.
4. `peek_set_visibility` changes a restricted page like any other; the approval card says what the
   change does to the people granted access.

## Alternatives considered

- **Keep `private` and `password` as PrivacyFence's own words and translate.** The assistant would
  see a value in `peek_list_pages` that it must not send back, and passwords no longer exist to
  translate to.
- **Default new pages to `signed-in`, what `private` meant before.** Peek's own default is
  `restricted`, and an assistant publishing a page should not open it to every Peek account unless
  asked.
- **Drop the `password` argument silently.** A caller that still sends a password would publish a
  page it believes is protected.

## Consequences

- Pages the assistant publishes are visible only to their owner, Peek admins and the people
  granted access until someone chooses otherwise.
- A client written for the old values breaks loudly: `password` gets a message, and any other
  unknown value gets `visibility must be public, signed-in or restricted.`
- The connector follows Peek's vocabulary; a further Peek rename needs a further change here.

## Verification

- `tests/unit/test_peek_client.py` (visibility normalisation, `upload_page` never sends a
  password) and `tests/unit/connectors/test_peek_connector.py` (`test_upload_defaults_to_restricted`,
  `test_upload_private_is_published_as_signed_in`, `test_upload_password_keyword_is_refused`,
  `test_set_visibility_password_keyword_is_refused`).
- `grep -n 'PASSWORD_MAX_BYTES\|"password"' src/privacyfence/connectors/peek.py src/privacyfence/peek_client.py`
  prints nothing.

## Related

- [`peek_client.py`](../../src/privacyfence/peek_client.py), [`connectors/peek.py`](../../src/privacyfence/connectors/peek.py)
- ADR [0158](0158-peek-rules-name-pages-and-cover-only-new-versions.md), ADR [0240](0240-privacyfence-manages-who-can-open-a-restricted-peek-page-behind-a-card.md)
