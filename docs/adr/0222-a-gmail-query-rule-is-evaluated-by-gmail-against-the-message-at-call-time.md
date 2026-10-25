# ADR 0222: A Gmail query rule is evaluated by Gmail against the message at call time

## Status

Accepted — 2026-10-22. Implemented.
Relates to ADR 0172 and ADR 0204.

## Context

A triage routine should read mail that matches a Gmail search such as
`in:inbox -category:promotions -label:"Proposed SPAM"`, and nothing else. The existing Gmail read scopes
name a sender or a sender domain. A search expression is only meaningfully evaluated by Gmail itself,
and policy modules do no I/O.

## Decision

- **A scope `gmail.query`** (read, download), whose value is one Gmail search expression, in the
  catalogue's extra scopes. It governs `gmail_get_message`, `gmail_get_thread` and
  `gmail_download_attachment`.
- **The value is kept whole.** The Settings value field does not split it on commas
  (`SINGLE_VALUE_GROUPS`). It is 1 to 500 characters with no control characters, balanced parentheses
  and an even number of double quotes, because the value is wrapped in parentheses when evaluated and
  `in:inbox) OR (in:anywhere` would widen the rule. A person sets it; a rule change needs a human
  confirmation and is refused in an unattended session.
- **Gmail evaluates it, in the connector, before gating.** The selector never calls Gmail. The
  connector takes the call's `gmail.query` rule values from the gate (the token's own rules under a
  scoped token) and, for at most ten of them, asks `GmailClient.messages_matching`, which runs
  `messages.list` with `q = "(<query>) (rfc822msgid:<id> OR …)"` and keeps the Gmail ids it returns.
  The matching rule values go into the call's arguments as `matching_queries`, and the selector matches
  when a rule value is in that list. The name is a connector-computed argument an agent cannot send to
  `check_policy`.
- **Fail closed.** More than 25 messages, a blank query, or a `Message-ID` outside a strict pattern
  gives no match. The ids are checked so a sender-chosen `Message-ID` cannot add search syntax, and the
  result is intersected with the Gmail ids so a duplicate `Message-ID` on another message cannot vouch
  for this one. An evaluation error counts as no match and the call falls back to review. The error
  text is fixed, because a Google error text carries the query and the `Message-ID`s.
- **A thread needs every message to match.** Reading a thread releases all of its messages, so any-match
  would release history outside the query. This is a product decision.
- **The scope resolves from the fetched item,** so `check_policy` answers `unknown`, and an uncovered
  Gmail read lists a `gmail.query` suggestion with no value.

## Alternatives considered

- **Evaluate the query inside the selector.** It would block the event loop on network I/O in a module
  that does no I/O.
- **Re-implement Gmail search locally.** It would never equal Gmail's.
- **List the query's results and look for the message.** Paging is unbounded.
- **Trust a caller-supplied match.** The agent could claim it.
- **Any message of a thread matches.** It releases history outside the query.

## Consequences

- A per-message operator such as `in:inbox` never matches a message the user sent (sent mail lacks
  `INBOX`), so a thread containing the user's own reply is not covered by `in:inbox …`.
- A search-index delay after a label change can make a just-labelled message not match yet.
- Each covered read adds one `messages.list` call per evaluated query.
- With a token's reads flag (ADR 0219), a matching read returns personal data without a card.

## Verification

- `tests/unit/test_gmail_client.py` (`TestMessagesMatching`, `TestRfc822Id`).
- `tests/unit/policy/test_scopes.py` (`TestGmailQuery`), `tests/unit/policy/test_catalogue.py`
  (`TestGmailQueryExtra`), `tests/unit/policy/test_value_format.py` (`TestGmailQueryValue`),
  `tests/unit/policy/test_suggest.py` (`TestGmailQuerySuggestion`).
- `tests/unit/test_gate.py` (`TestRuleValues`), `tests/unit/connectors/test_gmail_connector.py`
  (`TestGmailQueryArgs`), `tests/unit/test_gate_real_evaluator.py` (`TestGmailQueryRule`).
- `tests/unit/test_settings_controller.py`, `tests/unit/web/test_routes_org_settings.py` (the value is
  not split) and the live check in `scripts/qa_fixture_recorder.py`.

## Related

- ADR [0172](0172-one-stored-auto-accept-rule-per-scope.md), ADR [0204](0204-check-policy-names-the-rules-that-would-cover-an-uncovered-call.md), ADR [0219](0219-a-scoped-token-can-let-email-and-slack-reads-return-personal-data.md)
- [`gmail_client.py`](../../src/privacyfence/gmail_client.py), [`gmail.py`](../../src/privacyfence/connectors/gmail.py), [`scopes.py`](../../src/privacyfence/policy/scopes.py), [`gate.py`](../../src/privacyfence/gate.py)
