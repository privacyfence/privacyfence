# ADR 0333: A Drive folder-tree rule follows the folders a listing returned

## Status

Accepted — 2026-11-20. Implemented: `src/privacyfence/drive_listing_grants.py`, `src/privacyfence/policy/scopes.py` (`drive.folder_tree`), `src/privacyfence/policy/catalogue.py`, `src/privacyfence/policy/value_format.py`, `src/privacyfence/connectors/drive.py`.
Amends [ADR 0320](0320-drive-rules-match-on-file-tags-not-folder-ancestry.md) (Decision 4). Relates to [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), [ADR 0175](0175-an-approval-is-bound-to-the-whole-request-and-what-its-card-showed.md), [ADR 0222](0222-a-gmail-query-rule-is-evaluated-by-gmail-against-the-message-at-call-time.md), [ADR 0236](0236-a-scoped-token-holds-its-own-rules.md).

## Context

A Drive folder rule covers only the files whose direct parent is the folder. A workflow that reads a whole
folder tree, for example in n8n, can already list every level, because the listing tools are not gated, but each
file in a subfolder then asks for approval or, under a scoped token, is denied. Users asked for a rule that
follows the tree ([#1168](https://github.com/privacyfence/privacyfence/issues/1168)).

[ADR 0320](0320-drive-rules-match-on-file-tags-not-folder-ancestry.md) declined a recursive-folder rule: Drive
has no ancestor query, so deciding whether a file sits under a folder takes one `files.get` per level on every
gated call, and it rejected caching folder ancestry because a moved folder would be judged by stale data in a
trust decision. The tag scope it added needs one approval per tree and does not suit a workflow that only
walks a tree.

## Decision

1. **An opt-in scope, `drive.folder_tree`** ("Google Drive — a folder and the subfolders reached by listing
   it"), takes one or more Drive folder ids. Its verbs are read (also revisions and sheet values) and download
   only; it never covers write, move, copy or upload. Its value format accepts only a real folder id
   (10 to 100 characters of letters, digits, `_` and `-`) and refuses `root`: one listing of My Drive's top
   level would otherwise cover every top-level folder. It is added from Settings or in a scoped token's rules
   and never proposed on an approval card. The existing "Drive folder" rule is unchanged.
2. **Listing-derived grants.** When `drive_list_folder` lists a folder that is a rule's own folder, or a folder
   already granted under it, the connector remembers the ids of the sub*folders* that listing returned. The
   memory is in the process only, kept per scoped token (one shared key for an unscoped call) and per root
   folder, and is forgotten at restart. Listing tools stay ungated and return exactly what they returned before.
3. **Expiry.** A grant lasts one hour. A folder reached through another granted folder never outlives that
   folder's grant, so a whole chain is bounded by the last listing of the root folder. Reading a file never
   renews a grant; only a fresh listing of its parent does.
4. **Cap.** A root folder remembers at most 2000 folders. When full, new ones are not added and none is
   evicted, so the failure is a refusal, not a wider rule.
5. **Shortcuts are never followed.** Only items whose type is a folder are remembered.
6. **No ancestry is stored.** Every gated read re-reads the file's own parents. The rule matches when one of
   them is the rule's folder or a granted folder under it. A file moved out of the tree stops matching at once.
   Only the file's own granted parents are put into the gate's arguments, so two identical uncovered calls hash
   to the same approval key across retries ([ADR 0175](0175-an-approval-is-bound-to-the-whole-request-and-what-its-card-showed.md)).
   The connector computes that value, as for `gmail.query` ([ADR 0222](0222-a-gmail-query-rule-is-evaluated-by-gmail-against-the-message-at-call-time.md)),
   and a caller cannot supply it.
7. **ADR 0320 is amended.** Its decision that no recursive-folder rule is added is replaced by this opt-in
   scope. Its objection to caching is answered: nothing about the ancestry of a file is stored, only the ids of
   folders a listing returned, bounded by a chain expiry, and the file's own parents are read fresh on every
   call. The tag scope stays.

## Alternatives considered

- **Making the "Drive folder" rule recursive.** It would silently widen every existing rule and token, and needs
  an ancestor walk on each call.
- **Walking ancestors on every read.** It is the per-call cost ADR 0320 refused and the owner wanted to avoid.
- **Tagging.** It stays, but needs an approval for each tree and a tag on every new file.
- **Persisting the remembered folders or sharing them across tokens.** They would outlive the decision that
  created them, and a token would inherit another token's listings.
- **Granting from `drive_list_files` search results.** Search results are not rooted in the rule's folder, so
  they say nothing about where a folder sits.

## Consequences

- A workflow reads a whole tree with one rule on the top folder, with no extra Drive call: one in-memory lookup
  per gated read and per listing.
- A rule now reaches whatever its folder lists, one hour at a time. Whoever can create a subfolder or move a
  folder into the tree widens what a workflow can read, as whoever can put a file into a folder does for a
  direct folder rule.
- A folder moved out of the tree after a listing stays covered until its grant expires, at most one hour. A file
  moved out is refused at once.
- A restart forgets all grants. A workflow then asks again and, under a token, is refused until it lists the
  tree again.
- Grants are keyed by root folder, not by rule: removing and re-adding a rule within the hour, or adding the
  download verb to a read-only rule on the same folder, keeps the folders already remembered under that root.
- A listing larger than `max_results` grants only the folders it returned.
- A file with personal data still needs `allow_personal_data` or `allow_personal_data_reads` on the token.

## Verification

- `tests/unit/test_drive_listing_grants.py`, the `drive.folder_tree` cases of the policy tests (selector,
  catalogue, value format, sentence, suggestions), the Drive connector tests of the recorded listings and the
  stable gate arguments, and the real-evaluator test of a scoped token in `tests/unit/test_gate_real_evaluator.py`.

## Related

- Issue [#1168](https://github.com/privacyfence/privacyfence/issues/1168).
- [ADR 0320](0320-drive-rules-match-on-file-tags-not-folder-ancestry.md), [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), [ADR 0175](0175-an-approval-is-bound-to-the-whole-request-and-what-its-card-showed.md), [ADR 0222](0222-a-gmail-query-rule-is-evaluated-by-gmail-against-the-message-at-call-time.md), [ADR 0236](0236-a-scoped-token-holds-its-own-rules.md).
