# ADR 0240: PrivacyFence manages who can open a restricted Peek page, always behind a card

## Status

Accepted — 2026-10-27. Implemented.
Relates to 0158, 0239.

## Context

With `restricted` as the default for new pages (ADR 0239), an assistant could create pages that it
could then neither open up nor share, because the connector left restricted pages to Peek's
dashboard. In Peek's API, grants are ordinary owner actions: `GET`, `POST` and `DELETE` on
`/api/uploads/{slug}/grants` with `{"kind": "user" | "group", "email": …}`, and the versions list
is `GET /api/uploads/{slug}/versions`. See
<https://github.com/andras-tkcs/peek/blob/main/docs/src/content/docs/auth-access.md>.

A grant emails a new person the link and widens who can read the page. The grant list is a list of
email addresses.

## Decision

1. Four tools: `peek_list_versions` (auto-approved), `peek_list_grants` (review card),
   `peek_grant_access` and `peek_revoke_access` (approval popup). The grantee's email appears on
   the card and in the audit summary, never in a logger line.
2. No rule covers any grant tool. The `peek.page` scope excludes `peek.read_grants`,
   `peek.grant_access` and `peek.revoke_access` (`policy/catalogue.py`), so a "Peek — page" rule
   keeps compiling to exactly `peek.read_comments` and `peek.read_stats` for *read*, and the
   exclusion still holds if the scope ever gains an UPDATE verb. The grant list always gets its
   review card; a grant or a revoke always gets a popup.
3. ADR 0158's rule coverage is unchanged: a Peek page rule covers comments and stats (*read*) and
   new versions of a named page (*add* on `peek_upload_page`; ADR 0158 calls this "update"), and
   nothing else.
4. Neither grant write is marked destructive: a revoke is undone by a new grant.

## Alternatives considered

- **Keep restricted pages dashboard-only.** With `restricted` as the default, the assistant could
  create pages it could then neither open up nor share. Grants are ordinary owner actions in
  Peek's API, and every grant and revoke gets a card no rule can skip.
- **Let a Peek page rule cover grants or the grant list.** A grant sends an email to a new person
  and widens who can read the page. The grant list is email addresses, which the personal-data
  check would hold back anyway.

## Consequences

- An assistant can publish a page and share it with named people, each step approved by a person.
- Grants cannot be automated unattended; there is no "Always allow" for them.
- Group grants work only on a Peek server with Google group lookup; the tool says so when it does
  not.

## Verification

- `tests/unit/policy/test_catalogue.py` (`test_peek_page_read_compiles_comments_and_stats`,
  `test_peek_page_rules_never_include_grant_operations`).
- `tests/unit/policy/test_registry.py` (verb matrix), `tests/unit/test_write_effects.py`,
  `tests/unit/connectors/test_peek_connector.py` (the four handlers and their cards).
- `scripts/qa_fixture_recorder.py` `lifecycle_peek` exercises versions, grant and revoke weekly in
  `connector-live-check.yml`.

## Related

- [`policy/catalogue.py`](../../src/privacyfence/policy/catalogue.py), [`connectors/peek.py`](../../src/privacyfence/connectors/peek.py),
  [`peek_client.py`](../../src/privacyfence/peek_client.py)
- ADR [0158](0158-peek-rules-name-pages-and-cover-only-new-versions.md), ADR [0239](0239-the-peek-connector-uses-peeks-visibilities-and-new-pages-are-restricted.md)
