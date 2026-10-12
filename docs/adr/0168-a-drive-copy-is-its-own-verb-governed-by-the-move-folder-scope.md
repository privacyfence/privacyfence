# ADR 0168: A Drive copy is its own verb, governed by the move folder scope

## Status

Accepted — 2026-10-12. Implemented.

## Context

`drive_copy_file` copies one Drive file into another folder. Like a move, a copy has two ends: an
always-allow rule that checked only the source folder would let an agent copy a file out of an
approved folder into any folder the account can write to, and one that checked only the
destination would let it copy from anywhere.

## Decision

- **A new verb, `copy`.** It is in the write family and its scope subject is
  `source_and_destination`, like `move`. `drive_copy_file` maps to `Verb.COPY` and the operation key
  `drive.copy_file`.
- **The existing predicate `move_within_approved_folders` governs copies.** Its matcher already
  requires the file's parent and `destination_folder_id` to be in the rule's folder set, which is
  exactly what a copy needs. The folder scope lists `(Verb.MOVE, Verb.COPY)`, and the sandbox-folder
  grant gains `("drive.copy_file", "move_within_approved_folders")`.
- **One-shot.** `drive.copy_file` never arms the same-file grace window.
- **Existing rules are not widened** (ADR 0148, ADR 0166). A stored sandbox-folder or move rule does
  not gain `drive.copy_file`, and there is no migration. The user adds `copy` from the Auto-accept
  page or the popup's "also allow copy" widening.

## Alternatives considered

- **Reuse `Verb.MOVE`.** A "move" rule would silently also allow copies, and the popup's "also allow
  move" widening would understate its width.
- **`Verb.CREATE`.** Its scope subject is the container only, so a rule naming just the destination
  would copy a file out of any folder.
- **A new `copy_within_approved_folders` predicate.** The matcher would be identical, and scope
  selectors are parity-tested against the frozen v1 reference, which has no copy, so a new predicate
  would have to live in `NEW_SCOPE_SELECTORS` for no behavioural gain.

## Consequences

- A move rule and a copy rule on the same folders share a rule id (predicate, value and conditions,
  ADR 0074) and merge into one row carrying both verbs.
- A copy is owned by the user, so "files I own" and destination-folder read rules cover it; the PII
  check still applies to reading it, because a copy is not recorded as PrivacyFence's own write.
- Folders cannot be copied, because Drive's API has no folder copy; the tool refuses a folder before
  any popup.
