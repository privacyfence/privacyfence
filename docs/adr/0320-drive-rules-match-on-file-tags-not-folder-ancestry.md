# ADR 0320: Drive rules match on a file tag, not on folder ancestry

## Status

Accepted — 2026-11-17. Implemented: `src/privacyfence/drive_tags.py`, `src/privacyfence/policy/scopes.py` (`drive.tag`), `src/privacyfence/policy/catalogue.py`, `src/privacyfence/policy/value_format.py`.
Relates to [ADR 0172](0172-one-stored-auto-accept-rule-per-scope.md), [ADR 0222](0222-a-gmail-query-rule-is-evaluated-by-gmail-against-the-message-at-call-time.md), [ADR 0232](0232-miro-rules-are-per-board-and-set-from-settings-not-the-card.md).
Amended by [ADR 0333](0333-a-drive-folder-tree-rule-follows-the-folders-a-listing-returned.md), 2026-11-20.

## Context

A Drive folder rule covers only the files whose direct parent is the folder. Users asked for a rule that also
covers subfolders. Drive has no ancestor query: `'<id>' in parents` matches direct children only, so deciding
whether a file sits under a folder at any depth takes one `files.get` per level on every call. Nothing in the
repository walks ancestry, and that cost would fall on every gated Drive call.

Drive does let a file carry `properties`, small key/value pairs returned with the file's metadata. The connector
already holds the full `drive` scope, so reading and writing them needs no new consent.

## Decision

1. **A file tag is a Drive property.** A tag named `client-a` is the property `privacyfence-tag.client-a` with the
   value `1`. A tag name is 1 to 40 characters of lowercase letters, digits, `.`, `_` and `-`, starting with a
   letter or digit. Only properties with that key prefix, a valid name and the value `1` count as tags; every
   other property is ignored and never shown to the agent. `drive_get_file_metadata` and the listing tools show a
   file's `tags`.
2. **A new rule scope, `drive.tag`** ("Google Drive — files with a tag"), takes a tag name and matches when the
   fetched file carries it. It reads the metadata `get_file_metadata` already fetches, so it adds no Drive call
   per rule check. It is added from Settings and never proposed on an approval card, as for `gmail.query` and
   `miro.board`.
3. **Verbs.** Read, download, update, restructure and delete. Read also covers revisions; update also covers
   comments and Docs and Sheets writes. The scope **never covers move, copy or upload**: those quantify over a
   destination, which a tag on the source file does not describe.
4. **No recursive-folder rule is added.** A tag replaces it; tagging a whole folder tree is one approved call
   ([ADR 0322](0322-tag-writing-tools-are-popup-only-and-exempt-from-every-rule.md)). Existing rules change
   nothing.
5. **Derivation fix.** `extra_operations_for` matched an operation to a connector by its key prefix, so the
   `sheets.*` and `docs.*` operations of the Drive connector never derived for a Drive extra scope. It now uses
   the same connector lookup as `propose.connector_of_operation`. That is what lets restructure, delete and the
   Docs and Sheets writes derive for `drive.tag`; the operations of every existing extra scope are unchanged
   and a test pins that.

## Alternatives considered

- **A recursive-folder rule that walks ancestors.** One `files.get` per level per call, on every gated Drive
  call, with a depth cap to invent. The tag moves the work to the moment a user tags, once.
- **Caching folder ancestry.** A moved folder or a file moved between trees would be judged by stale data in a
  trust decision.
- **Letting the tag scope cover move and copy.** The tag says nothing about where the file goes, so a tagged file
  could be moved or copied out of a trusted place with no check on the destination.
- **Proposing the scope on approval cards.** The tag value is not on the card; the user chooses it deliberately
  in Settings.

## Consequences

- A rule can cover files at any depth with no folder walking and no extra Drive call.
- A file's membership in a rule is visible and editable on the file itself, wherever it sits.
- Files added to a tagged folder later do not inherit its tag.
- Who can set a tag decides who can widen a rule, see
  [ADR 0321](0321-drive-tags-are-plain-properties-and-collaborators-can-forge-them.md).

## Verification

- `tests/unit/test_drive_tags.py`, the `drive.tag` cases of the policy tests (selector, catalogue,
  value format, sentence), and the test pinning `extra_operations_for` for the existing extra scopes.

## Related

- Issue [#1147](https://github.com/privacyfence/privacyfence/issues/1147), plan [`docs/1147/plan.md`](../1147/plan.md).
- [ADR 0321](0321-drive-tags-are-plain-properties-and-collaborators-can-forge-them.md), [ADR 0322](0322-tag-writing-tools-are-popup-only-and-exempt-from-every-rule.md).
