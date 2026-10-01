# ADR 0118: An approver sees Atlassian's name for every mentioned or assigned account

## Status

Accepted — 2026-09-30. Implemented: `src/privacyfence/connectors/jira.py`,
`src/privacyfence/connectors/confluence.py`, `src/privacyfence/atlassian_users.py`.

## Context

Jira and Confluence writes can now carry real `@mentions`, and Jira writes can assign an issue. The
agent writes a mention as `@[Name]` followed by the account id in parentheses. The label in brackets is the agent's own text, so a
card that showed it would let the agent present one person's account id under another person's
name, and the approver would approve a notification to someone they did not expect.

## Decision

The approval card names each mentioned or assigned account with the name Atlassian returns for its
id, never the agent's label. This covers the card title, the body preview and the `Mentions` and
`Assignee` preview rows. Only the audit record's `raw_data` and `args` keep the agent's text; every Jira
mention node sent to Atlassian, in a description, a comment or a rich-text custom field, carries the
directory's name in its `attrs.text`.

`jira_update_issue`'s `custom_fields` follows the same rule:

- A field that has a dedicated parameter (`summary`, `description`, `priority`, `assignee`) is refused
  in `custom_fields`, so it cannot bypass that parameter's name resolution.
- A user field, or a multi-user picker, accepts only `{"accountId": id}` or a bare id string, which is
  normalised to the object form. Any other key (`id`, `name`, `key`, `displayName`, `emailAddress`) is
  refused with a message naming the accepted shape.
- Every account id found at any depth of any custom value, including ADF `mention` nodes, is resolved
  and named like the others, and an unresolvable one is refused before the card.
- The card shows what is written. A rich-text (ADF) value is rendered in full: text verbatim (mention markup typed
  mention markup typed into a text node stays literal text), mentions as directory names, every link target next to
  its text, every `inlineCard`, `blockCard` and `embedCard` URL and `data`, and a placeholder carrying
  the attributes of media, emoji, status and date nodes. Only a fixed list of node and mark types can
  be shown completely; a value containing any other type is refused, naming it, before the card. A
  plain value is shown with people replaced by their names.

An id in mention markup, or an `assignee_account_id`, that cannot be resolved is refused with
`Unknown Atlassian account id(s): …` before any approval card appears.

Mentions already present in a Confluence body in storage format are shown, not refused. Raw
storage-format mentions already worked before this change and may be unresolvable on a
Confluence-only site, where refusing them would make existing pages uneditable. The card labels an
unresolved one as `unknown account <id>`, and a legacy `ri:userkey` / `ri:username` mention (no account
id) as `unrecognised user mention(s)`. Storage tags are found with an HTML tokenizer, not a regex.

## Alternatives considered

- **Show the agent's label and trust it.** Rejected. The label is unverified text chosen by the
  agent.
- **Refuse every unresolvable id, including raw storage mentions.** Rejected, for the reason above.
- **Skip the lookup and show raw ids.** Rejected. The approver cannot tell who an id is.

## Consequences

- A write with a mention costs one cached lookup before the card.
- On a site whose users cannot browse other users, mention writes fail until the permission is
  restored. The error says so.
- `markup_to_storage` rewrites the markup anywhere in a Confluence body, including inside an
  attribute value or a code block, so a literal mention-shaped string there is refused as an unknown
  id.

## Verification

`tests/unit/connectors/test_jira_connector.py` and
`tests/unit/connectors/test_confluence_connector.py`, the write-preview and unknown-id tests.

## Related

- [ADR 0117](0117-atlassian-account-ids-resolve-through-a-lazy-shared-cache.md)
- [ADR 0119](0119-atlassian-find-users-is-auto-approved-and-returns-no-email.md)
