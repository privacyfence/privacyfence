# ADR 0339: Jira parent is set with `parent_key` and Epic Link is refused

## Status

Accepted — 2026-11-27. Implemented.

## Context

Issue [#1192](https://github.com/privacyfence/privacyfence/issues/1192): the Jira connector could not set or
change an issue's parent. `custom_fields {"Parent": "AINC-24"}` sent a bare string, Jira answered 400
"data was not an object", and the AI saw only "update_issue('AINC-25') failed: data was not an object".
`atlassian-python-api` builds its error text from the `errors` values and drops the field names, so the AI
could not tell which field failed.

Atlassian told developers to send `parent` instead of Epic Link and Parent Link when creating or editing
issues, in company-managed and team-managed projects alike, and the old behaviour may be switched off at any
time ([deprecation notice](https://community.developer.atlassian.com/t/deprecation-of-the-epic-link-parent-link-and-other-related-fields-in-rest-apis-and-webhooks/54048),
2021-11-30, updated 2022-10-07).

## Decision

- `jira_create_issue` and `jira_update_issue` take an optional `parent_key`, sent as
  `{"parent": {"key": "<KEY>"}}`. It is validated as an issue key, an issue cannot be its own parent, and the
  parent is read before the approval card, which shows its key and a truncated summary.
- `custom_fields` refuses Parent, Epic Link and Parent Link by name and by field kind, and points to
  `parent_key`.
- Clearing a parent is not offered. Empty `parent_key` leaves the parent unchanged.
- A failed create or update reports Jira's `errorMessages` and per-field `errors`, read from the response.

## Alternatives considered

- **Epic Link support.** Atlassian deprecated it in favour of `parent`
  ([2021-11-30](https://community.developer.atlassian.com/t/deprecation-of-the-epic-link-parent-link-and-other-related-fields-in-rest-apis-and-webhooks/54048)),
  and its field id differs per site.
- **A sentinel or flag that clears the parent.** The only evidence that `{"parent": null}` works is one
  unofficial post ([2023-02-28](https://community.developer.atlassian.com/t/jira-api-edit-issue-with-null-parent-results-in-error-we-couldnt-save-your-changes-copy-your-content-then-try-reloading-the-page/52514)),
  which also reports 500 on issues without a parent. It would ship an unverified Jira call.
- **A bare `custom_fields` fix for "Parent".** It would miss Epic Link and Parent Link.

## Consequences

- A site where the unofficial clearing works can have it added later with its own ADR.
- The parent read before the card is ungated and may be in another project. The approver sees only its key
  and summary; the AI's result does not carry it.
- A scoped token or Always-allow rule scopes on the edited issue's project, never on the parent's: the gate
  `args` stay `{"issue_key": ...}`.

## Related

- [`jira.py`](../../src/privacyfence/connectors/jira.py), [`jira_client.py`](../../src/privacyfence/jira_client.py)
