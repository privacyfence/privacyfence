# ADR 0322: The tag-writing tools are popup-only and exempt from every rule

## Status

Accepted — 2026-11-17. Implemented: `src/privacyfence/policy/registry.py` (`RULE_EXEMPT_OPERATIONS`), `src/privacyfence/policy/engine.py`, `src/privacyfence/policy/propose.py`, `src/privacyfence/policy/catalogue.py`, `src/privacyfence/auto_accept.py`, `src/privacyfence/connectors/drive.py`.
Relates to [ADR 0077](0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md), [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), [ADR 0236](0236-a-scoped-token-holds-its-own-rules.md).

## Context

A tag rule ([ADR 0320](0320-drive-rules-match-on-file-tags-not-folder-ancestry.md)) trusts whatever carries the
tag. If the agent could tag a file without a popup, it could tag its way into any tag rule. The two tools that
write tags are `drive_set_file_tags` (one file) and `drive_tag_folder_tree` (every file under a folder, up to
200 files and 10 levels).

## Decision

1. **Both tools always show an approval popup.** `drive_set_file_tags` names the file and the tags to add and
   remove. `drive_tag_folder_tree` shows how many files change and lists them, and applies exactly the listed
   snapshot after approval. A folder over the caps changes nothing.
2. **No rule can cover them.** `RULE_EXEMPT_OPERATIONS` holds `drive.set_file_tags` and `drive.tag_folder_tree`.
   The engine refuses to match a rule for these operations and `preflight` answers "requires review" before any
   rule is read, so a custom row, a hand-edited `settings.yaml`, a scoped token's rule and an upstream approval
   cannot cover them either. Rule derivation and suggestions subtract the set, and the same-file grace window
   excludes both keys.
3. **Enforced in the engine**, not by per-scope excludes alone: an exclude on a scope does not bind a custom row.
4. **A scoped token** whose rules list these tools is still denied the popup-less path and needs an approval;
   nothing else changes for tokens.
5. **Tags are not an own write.** Tagging does not mark a file as created by the session.

## Alternatives considered

- **Per-scope `excludes` only.** A custom rule row, which no scope excludes bind, would still cover the tools.
- **Auto-accepting tagging inside already-trusted folders.** The agent could extend a tag rule's reach into
  files the user never named.
- **A rule scope for tagging with its own verb.** Tagging is the act that widens every other rule; it is the one
  operation that must stay a human decision each time.

## Consequences

- Tagging is never silent; a tag rule only pays off for the reads and writes that follow.
- The exemption is a property of the operation, so a future rule source is covered by it without change.
- Tagging a large tree costs one approval, not one per file.

## Verification

- `tests/unit/policy/test_rule_exempt_operations.py`, `tests/unit/policy/test_registry.py`, and the tag tool
  tests in `tests/unit/connectors/test_drive_connector.py`.

## Related

- Issue [#1147](https://github.com/privacyfence/privacyfence/issues/1147), plan [`docs/1147/plan.md`](../1147/plan.md).
- [ADR 0320](0320-drive-rules-match-on-file-tags-not-folder-ancestry.md), [ADR 0321](0321-drive-tags-are-plain-properties-and-collaborators-can-forge-them.md).
