# ADR 0134: Extra Jira fields are read through a separate reviewed search with no scope

## Status

Accepted — 2026-10-09. Implemented: `src/privacyfence/connectors/jira.py`,
`src/privacyfence/jira_client.py`, `src/privacyfence/auto_accept.py`.

## Context

`jira_search_issues` is auto-approved because it returns metadata only: key, summary, status, type,
priority, people, labels and dates. An issue's description and comments are held back for the
reviewed `jira_get_issue`. Users want other fields, such as Story Points, Sprint, due date or fix
versions. A custom field can hold anything: a multi-line "Acceptance criteria", a customer name, a
free-text "Root cause". The user has to see those values before the agent does. A tool has exactly
one gate in `TOOL_TO_GATE`, and the tools reference, the `privacyfence_check_policy` preflight and
the gate invariant tests rely on that.

## Decision

- `jira_search_issues_with_fields` is a separate tool with gate `review`. It runs a JQL search and
  adds the requested fields to each issue; the approval card shows every issue and every requested
  value before anything is released.
- Its operation is `jira.search_issues_with_fields` with `Verb.SEARCH` and no scope. The Jira scope
  catalogue covers `READ`, `CREATE`, `COMMENT`, `UPDATE` and `TRANSITION`, and a JQL query names no
  single project, so no standing rule can auto-accept it: every call shows a card.
- `jira_get_issue` takes the same optional `fields` parameter and keeps operation
  `jira.read_issue`, with or without it. Its project, reporter and assignee rules still auto-accept
  it, because asking for more fields of the same issue does not change whose issue it is.
- `jira_search_issues` keeps its shape and gate, and asks Jira only for the fields it returns.

## Alternatives considered

- **Extra fields on the auto-approved search.** Content would skip review, which breaks the rule
  that only metadata runs without a card.
- **Auto-approved, but refusing free-text field types.** Judging content by schema type is brittle.
  Single-line text, labels and option values can carry content too, and an admin can add a field
  type the allowlist does not know.
- **One tool whose gate depends on its arguments.** A tool has one gate in `TOOL_TO_GATE`; the tools
  reference, the policy preflight and the gate invariant tests all assume it.

## Consequences

Every extra-field search costs a review card, and a user cannot write a standing rule to waive it.
The agent can still read extra fields of one issue without a card where an existing `jira.read_issue`
rule already covers that issue. Agents need `jira_list_fields` (ADR 0135) to find field ids.

## Verification

`TestSearchIssuesWithFields` and `TestGetIssue` in `tests/unit/connectors/test_jira_connector.py`,
and the `jira_get_issue`, `jira_search_issues_with_fields` rows in `TOOL_TO_OPERATION` and
`TOOL_TO_GATE` in `src/privacyfence/auto_accept.py`.

## Related

- [ADR 0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md)
- [ADR 0116](0116-list-tools-page-inside-the-client-and-keep-their-return-shape.md)
- [ADR 0135](0135-the-jira-field-list-is-auto-approved.md)
