# ADR 0116: Atlassian account ids resolve through a lazy cache that Jira and Confluence share

## Status

Accepted — 2026-09-30. Implemented: `src/privacyfence/atlassian_users.py`,
`src/privacyfence/jira_client.py`, `src/privacyfence/confluence_client.py`.

## Context

Jira and Confluence identify people by Atlassian account id. A mention in a Jira description or a
Confluence page, or the author of a page, reaches the agent and the approver as an opaque id such
as `5b10ac8d82e05b22cc7d4ef5`, so nobody can tell who is meant. Slack solves the same problem with
a weekly snapshot of the whole workspace roster (commit 52768c86), which also lets it match names
to ids offline.

Jira's user API (`/rest/api/3/user/bulk`) resolves ids to names for every Atlassian product on a
site, and the `read:jira-user` scope is already part of the Jira scopes every token holds.

## Decision

A new module, `atlassian_users.py`, holds one account-id → display-name directory that both
clients share. It resolves only the ids that actually appear, in batches, through Jira's user
API, and remembers each one per user for 7 days, optionally on disk in
`atlassian_user_cache.json`. A failed lookup is remembered for an hour, and a failed fetch is not
repeated for five minutes, so a site without Jira costs one attempt per cooldown.

Confluence calls Jira's API with the token it already has and retries only on a 401.

## Alternatives considered

- **A weekly full-directory walk, like Slack's.** Rejected. On a large site it is many calls, and it
  would put the whole company's roster on disk. Slack's benefit from it, offline name matching for
  participant filters, is covered here by the live `*_find_users` tools
  ([ADR 0118](0118-atlassian-find-users-is-auto-approved-and-returns-no-email.md)).
- **A new `read:user:confluence` scope for Confluence.** Rejected. It would force every existing
  user to re-authenticate with Atlassian.

## Consequences

- On a Confluence-only site (no Jira product) lookups fail. Names fall back to raw account ids, and
  mention markup cannot be used there.
- A person renamed in Atlassian shows the old name for up to a week, unless the agent calls
  `jira_refresh_user_cache` or `confluence_refresh_user_cache`.
- The cache file holds names and ids only, no email addresses.

## Verification

`tests/unit/test_atlassian_users.py`, and the `TestUserLookups` classes in
`tests/unit/test_jira_client.py` and `tests/unit/test_confluence_client.py`.

## Related

- [ADR 0117](0117-an-approver-sees-atlassians-name-for-every-mentioned-or-assigned-account.md)
- [ADR 0118](0118-atlassian-find-users-is-auto-approved-and-returns-no-email.md)
