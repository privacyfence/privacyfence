# ADR 0128: Plugin source reads never truncate

## Status

Accepted — 2026-10-08. Implemented: `src/privacyfence/plugins/cursors.py`,
`src/privacyfence/plugins/source_ops.py`, `src/privacyfence/jira_client.py`
(`search_issues_page`), `src/privacyfence/calendar_client.py` (`list_events_page`).
Amends [ADR 0123](0123-the-plugin-source-api-is-ungated-but-audited-without-content.md).
Salesforce report paging: [ADR 0132](0132-salesforce-reports-page-by-a-unique-key-column.md).

## Context

A plugin that copies data out of a service needs all of it. ADR 0123's source API cut results
silently or failed them: Jira and Calendar followed the provider's page tokens up to ten pages and
dropped the rest, Sheets and Confluence results over the limit failed, and a plugin could not tell
"that was everything" from "that was all I will give you". A copy that quietly misses rows is worse
than one that fails.
Requested in [the design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618).

## Decision

Every source operation returns all of its data, or a page and a `next_cursor`. A size limit is a
page size, not a failure. `next_cursor` is `null` exactly when nothing is left.

- The cursor is opaque and is bound to the operation and the parameters that produced it. A cursor
  used with other parameters is `invalid_params`. The binding stops a cursor being carried over to
  a different query by mistake; it is neither signed nor secret, because the plugin reads with its
  own rights either way.
- `jira.search` and `calendar.list_events` page through the provider's own tokens, one provider
  request per page, with a `page_size` parameter. The 1.0 `max_results` is kept as an alias.
- `sheets.get_values` splits by rows and `confluence.get_page` by characters of the body, each page
  as large as fits.
- `drive.download` pages by byte offset ([ADR 0129](0129-drive-binary-downloads-for-plugins-are-http-range-reads-with-no-size-cap.md)).
- The only size refusal left is a single record larger than a page (`payload_too_large`), and
  Salesforce report runs, which do not page yet:
  [privacyfence/privacyfence#854](https://github.com/privacyfence/privacyfence/issues/854).

The change is minor: protocol 1.1 adds optional parameters, and a 1.0 plugin keeps working and
now receives the rest of a result through the cursor.

## Alternatives considered

- **Raise the limits.** Rejected. Any limit is still a silent cut for a larger source.
- **Let the plugin pass provider tokens through.** Rejected. It would expose the provider's paging
  to the plugin and tie the protocol to each provider.
- **Sign the cursor.** Rejected. It is not a security boundary; the plugin may read anything the
  operation allows.

## Consequences

- A plugin must follow `next_cursor`. The SDK's `ctx.source.pages` does it, and `collect` gathers a
  list for the two operations that return lists.
- An operation's pages may change between calls when the source changes; the cursor does not
  freeze the source.
- Salesforce report runs can still fail on a large report until that operation pages.

## Verification

`tests/unit/plugins/test_cursors.py`, `tests/integration/test_plugin_paging.py` (every operation
through a real plugin), the client tests for `search_issues_page` and `list_events_page`, and
`tests/unit/plugin_sdk/test_plugin.py` (the page iterator).

## Related

- [The design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618)
- [ADR 0123](0123-the-plugin-source-api-is-ungated-but-audited-without-content.md)
