# ADR 0199: Jira and Confluence "mine" rules compare Atlassian account ids, never names or emails

## Status

Accepted — 2026-10-17. Implemented.

## Context

The scope rules `i_am_reporter`, `i_am_assignee` (Jira) and `i_am_author` (Confluence) decide whether
a request is about the user's own work, and an auto-accept row can hang off them. They matched on a
substring of a display name, and the Confluence rule never matched at all. A display name is chosen
by the person and is not unique, so a colleague whose name contains the user's name could be treated
as the user. Atlassian's own stable identifier for a person is the `accountId`, and both the Jira
issue (`reporter.accountId`, `assignee.accountId`) and the Confluence v2 page (`authorId`) carry it.

## Decision

The three matchers in `src/privacyfence/policy/scopes.py` compare the account id carried by the
record with `ctx.my_account_id`, as an exact, case-sensitive string comparison. They never read
`ctx.my_email` or a display name, and they never match when `my_account_id` is empty.

- The id of the signed-in user is fetched once from `/myself` at sign-in (`_fetch_account_id` in
  `atlassian_oauth.py`) and stored as `account_id` in the token record. `JiraClient.check_connection`
  backfills it for installs that signed in earlier, and `daemon_main.build_connectors` shares it with
  the Confluence and Assets clients. A token refresh in any of the three clients keeps the stored id.
- `JiraIssue` carries `reporter_account_id` and `assignee_account_id` as plain attributes, not
  dataclass fields, so what the agent receives is unchanged. `JiraConnector` passes them in the rule
  context's `raw_data`. `ConfluencePage.author` already holds the v2 `authorId`.
- `ReviewContext` gains `my_account_id` and `gate.gated_call` takes it as a keyword; the Jira and
  Confluence connectors set it from their client.
- No setting, no change to a rule's stored form or content-derived id.

## Alternatives considered

- **Compare the account id inside the Jira client and hand the matcher a boolean.** The matchers are
  the one place identity rules live and are unit-tested against the rule table; a boolean computed
  elsewhere escapes that.
- **Read `/myself` on every rule evaluation.** One extra request per read, for a value that does not change.
- **Reuse `my_email` for the id.** It would make `ctx.my_domain` and the Google and Slack rules that
  read `my_email` ambiguous.

## Consequences

- A rule can no longer match someone whose display name merely contains the user's.
- `i_am_author` now matches on Confluence, where it never did. An install with only Confluence enabled
  gets the id at its next Atlassian sign-in; until then the rule does not match, which is the safe
  direction and the behavior it already had.
- An install with Jira enabled gets the id on its first start after upgrading.
- Account ids are opaque, so a rule cannot be written by hand from a name.

## Verification

- `tests/unit/policy/test_scopes.py` exercises the three matchers, including empty ids and near-miss names.
- `tests/unit/policy/_v1_reference.py` records the deliberate divergence from the v1 matchers.
- `tests/unit/test_atlassian_oauth.py`, `tests/unit/test_jira_client.py`,
  `tests/unit/test_confluence_client.py`, `tests/unit/test_assets_client.py`, `tests/unit/connectors/test_jira_connector.py`,
  `tests/unit/connectors/test_confluence_connector.py`, `tests/unit/test_gate_real_evaluator.py` and
  `tests/unit/test_daemon_main.py` cover fetching, carrying, backfilling and passing the id.

## Related

- Issue [#923](https://github.com/privacyfence/privacyfence/issues/923).
- [ADR 0117](0117-atlassian-account-ids-resolve-through-a-lazy-shared-cache.md) and
  [ADR 0118](0118-an-approver-sees-atlassians-name-for-every-mentioned-or-assigned-account.md): the
  other places account ids are used.
