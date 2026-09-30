# ADR 0118: `jira_find_users` and `confluence_find_users` are auto-approved and return no email

## Status

Accepted — 2026-09-30. Implemented: `src/privacyfence/connectors/jira.py`,
`src/privacyfence/connectors/confluence.py`, `src/privacyfence/auto_accept.py`.

## Context

To mention or assign someone, the agent needs an account id, which only a directory search gives
it. The search could sit behind an approval card, or run without one.

## Decision

Both tools are auto-approved. They search people by name and return display names and account ids
only, never an email address (an email-shaped display name, as on Jira Service Management
customer accounts, is replaced by `Customer account <last 4 of the id>`). That is the same sensitivity as `jira_search_issues` returning
assignee names without a card. The two refresh-cache tools are auto-approved for the same reason.

No privacy-filter category is added. Mention names in a body sit inside content the human approves
at the review gate.

## Alternatives considered

- **Review every lookup.** Rejected. A mention would cost two cards, one of which shows nothing the
  second does not, since the write card already names the person
  ([ADR 0117](0117-an-approver-sees-atlassians-name-for-every-mentioned-or-assigned-account.md)).
- **Return emails so the agent can tell namesakes apart.** Rejected. An email is personal data the
  agent does not need to write a mention.

## Consequences

- An agent can probe the directory for the names of people it does not already know. The results are
  limited to what Atlassian shows the signed-in user.
- Results are remembered, so a later write resolves them without another lookup.

## Verification

The `TestFindUsers` classes in `tests/unit/connectors/test_jira_connector.py` and
`tests/unit/connectors/test_confluence_connector.py`, and the tool gates in
`src/privacyfence/auto_accept.py`.

## Related

- [ADR 0116](0116-atlassian-account-ids-resolve-through-a-lazy-shared-cache.md)
- [ADR 0117](0117-an-approver-sees-atlassians-name-for-every-mentioned-or-assigned-account.md)
