# ADR 0135: The Jira field list is auto-approved

## Status

Accepted — 2026-10-09. Implemented: `src/privacyfence/connectors/jira.py`,
`src/privacyfence/jira_client.py`, `src/privacyfence/auto_accept.py`.

## Context

The agent does not know a site's field ids (`customfield_10016`, or `cf[10016]` in JQL), so it needs
to turn the names people use into ids before it can ask for extra fields (ADR 0134).

## Decision

`jira_list_fields` has gate `auto`. It lists the site's fields: name, id, whether it is custom, type
and the names JQL accepts. Field names, ids, types and JQL names are the site's schema, not issue
content: `GET /rest/api/3/field` returns no values, emails or account ids. ADR 0119 made the same
call for people lookups.

## Alternatives considered

- **A review card for each lookup.** Rejected: it would add a card that shows nothing the search
  card does not, since the later search card lists the requested fields and their values.

## Consequences

A lookup runs without a card. Field names can still reveal how a site organises its work, but the
same schema is visible to anyone with access to the Jira project.

## Verification

`TestListFields` in `tests/unit/connectors/test_jira_connector.py`; the `jira_list_fields` row in
`TOOL_TO_GATE` in `src/privacyfence/auto_accept.py`.

## Related

- [ADR 0119](0119-atlassian-find-users-is-auto-approved-and-returns-no-email.md)
- [ADR 0134](0134-extra-jira-fields-go-through-a-reviewed-search.md)
