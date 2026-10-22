# ADR 0218: A Gmail label rule can allow archiving a message that carries the label

## Status

Accepted — 2026-10-22. Implemented.
Relates to ADR 0172 and ADR 0204.

## Context

An unattended inbox-triage routine labels the messages it has sorted and then archives them. Every call
has to pass on an auto-accept rule. `gmail_archive_message` has the verb `move`, and the only Gmail
rules that could cover it were `i_am_sender` and `trusted_sender_domain`, which say nothing about
whether the message was triaged.

The `gmail.label` scope group already had a selector that fits, `label_match`: it compares a rule's
label names with the labels of the fetched message, case-insensitively. No catalogue entry used it.
`GmailMessage.labels` holds label ids (`INBOX`, `UNREAD`, `Label_123`), while a rule value is a name.

## Decision

- **The `gmail.label` group gains the verb `move`** through `label_match`, a second scope entry of the
  same group. One stored row with the verbs `update` and `move` compiles to two engine rules:
  `label_name_allowlist` for the label operations and `label_match` for `gmail.archive_message`.
- **The check runs against the message's live labels.** `_archive_message` already fetches the message
  on every call. It maps `message.labels` (ids) to user label names with `GmailClient.label_names` and
  passes the message with those names to the gate. The request carries only `message_id`; nothing the
  agent sends can name a label. A label id Gmail no longer lists is dropped, and a lookup error fails
  the call, so nothing is archived.
- **Only user labels count.** `label_names` keeps labels whose type is `user`. A Gmail system label
  (`INBOX`, `UNREAD`, `STARRED`, `CATEGORY_*`) is never among the names a rule is compared with, so a
  rule whose value is `INBOX` never allows an archive.
- **A card never proposes the scope.** A reviewer cannot pick which label of the message should matter,
  so the entry proposes no value; the rule is configured deliberately in Settings.
- **`privacyfence_check_policy` stays argument-only.** It never fetches (ADR 0204), so for an archive
  covered by a label rule it answers `unknown` and lists a `gmail.label` / `move` suggestion with no
  value.
- **The label is not an independent check on a steered agent.** The same row grants `update` and
  `move`, so whatever the agent can label it can archive: the label gates the archive, but a steered
  agent can apply the label first. The safety is that archiving is reversible and leaves the message
  unread.
- **Existing scoped tokens do not gain `move`.** A token pins the operations its rules had when it was
  created (ADR 0164); after editing a rule, create a new token.

## Alternatives considered

- **Match on label ids.** A rule would carry opaque ids and break when a label is recreated.
- **Make `GmailMessage.labels` carry names.** The field is returned to the agent by `gmail_get_message`;
  changing it changes that tool's output.
- **A new predicate.** `label_match` already has the right semantics and an equivalence-tested selector.
- **Make `check_policy` fetch the message.** It would give up the rule that the prediction makes no
  call (ADR 0204).

## Consequences

- Every archive makes one extra `labels.list` call, also when no `label_match` rule exists. Archiving
  is low-volume; accepted.
- The add-label card's "Always allow" widening list offers "also allow move" for a label, because
  widenings follow the group's verbs. It is an explicit chip the reviewer chooses.
- A hand-written custom row that uses `label_match` on a read operation still works in the engine but
  shows as allowing nothing in Settings.
- A just-applied label is seen at once, because the check reads the live labels.

## Verification

- `tests/unit/policy/test_propose.py` (`TestGmailLabelMove`), `tests/unit/policy/test_scope_rows.py`
  (the compiled rules), `tests/unit/policy/test_suggest.py` (`TestGmailArchiveSuggestion`).
- `tests/unit/test_gmail_client.py` (`TestLabelNames`).
- `tests/unit/test_gate_real_evaluator.py` (`TestArchiveByLabel`, including the `unknown` preflight).
- `tests/unit/connectors/test_gmail_connector.py` (`TestArchiveByLabel`).

## Related

- ADR [0172](0172-one-stored-auto-accept-rule-per-scope.md), ADR [0204](0204-check-policy-names-the-rules-that-would-cover-an-uncovered-call.md), ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)
- [`propose.py`](../../src/privacyfence/policy/propose.py), [`scopes.py`](../../src/privacyfence/policy/scopes.py), [`gmail.py`](../../src/privacyfence/connectors/gmail.py), [`gmail_client.py`](../../src/privacyfence/gmail_client.py)
