# ADR 0171: Auto-accept rules use nine verbs

## Status

Accepted — 2026-10-13. Implemented.
Amends ADR 0170. Copy is folded into *move*: it is still governed by the `move_within_approved_folders` scope, and a move rule saved before this change shows `drive_copy_file` as a tool it does not cover yet.

## Context

The verb vocabulary had grown to 19 members, and several overlapped: *create* and *draft*, *update*
and *format*, *comment*, *label*, *complete*, *transition* and *share* were all "change something
that exists", *copy* and *archive* were moves, and *configure* was a restructure. Grist and Peek
rules showed bracketed labels (for example "create [import CSV]") to explain what a bare verb meant
there. A person choosing verbs for a rule had to learn which of 19 words reached which tool, and
the same word meant different tools under different scopes.

Verbs are not stored. A stored rule holds operation keys (ADR 0074), and the verbs a rule shows are
derived from them, so changing the vocabulary changes no stored data.

## Decision

- **Nine verbs, in four families.** `read`, `download` and `search` (read); `add`, `update`,
  `restructure` and `move` (write); `send` (send); `delete` (destructive). The registry
  (`policy/registry.py`) maps every governed tool to exactly one of them, and the catalogue derives
  each scope's operations from that mapping.
- **Merged verbs.** *create* and *draft* become `add`. *format*, *comment*, *label*, *complete*,
  *transition* and Calendar/Peek *share* (visibility) become `update`. *copy* and *archive* become
  `move`. *configure* becomes `restructure`. Creating a Slack group chat, formerly *share*, becomes
  `send`.
- **Older names keep working as input.** `registry.LEGACY_VERB_ALIASES` maps each removed name to its
  successor, and `catalogue.parse_verbs` accepts either, mapping and de-duplicating in first-seen
  order. A `privacyfence_propose_policy_change` call or a `privacyfence-sync` recipe written with the
  old names therefore still lands. The propose tool's description lists the nine current verbs and
  says only that older names are mapped, without listing them: its description is limited to 1024
  characters.
- **No bracketed labels.** `PolicyExtraScope` lost `verb_labels`; Settings shows the bare verb.
- **Three new `excludes` keep a merged verb from reaching more than it should.** Gmail's
  unconditional drafting scope excludes `gmail.create_label`; a Slack recipient scope excludes
  `slack.create_group_chat` (which an `approved_recipient` check could never match); a contacts
  label scope excludes `contacts.edit`. The two unconditional extra scopes were renamed
  `gmail.filters` and `slack.group_chats`, with `catalogue.GROUP_ALIASES` keeping the old ids
  (`gmail.configure`, `slack.share_anything`) working.
- **Copy folds into move (amends ADR 0170).** A Drive folder rule's `move` verb now covers
  `drive_move_file` and `drive_copy_file` under `move_within_approved_folders`, and still needs both
  the source and destination folder in the rule. A move rule written before this change lacks
  `drive.copy_file`, so it shows the tool as not covered yet and offers **Update rule**; nothing
  widens it automatically (ADR 0166).
- **Some merged verbs are wider than the verbs they replace, and that is accepted.** A grant that
  allowed only Tasks *complete* or *edit*, or only Jira *comment*, *update* or *transition*, now
  reaches its sibling update tools too (`tasks_update_task` with complete; `tasks_complete_task` and
  `tasks_uncomplete_task` with edit; the other two Jira update tools with each Jira verb).
  `tests/unit/policy/test_propose.py` pins every such pair in `_MERGED_VERB_EXTRAS`, so the
  widening is a recorded list and not a surprise. It affects what a *newly derived* rule covers;
  rules already stored keep their operation keys.

## Alternatives considered

- **Keep 19 verbs and group the chips in the UI.** The vocabulary the engine, the bridge and the
  recipes use would stay as confusing as before, and the grouping would only hide it.
- **Separate verbs for share/send and copy/move.** Visibility changes are updates to an existing
  thing, not a message to a recipient, and a copy needs the same two-folder check as a move.
  Separate verbs would keep the overlap this ADR removes.

## Consequences

- A person chooses among nine words, and the same word means the same family of tool under every
  scope.
- Old proposals, recipes and `privacyfence_list_policy` consumers keep working through the aliases.
- A `move` verb on a Drive folder rule now includes copying a file into another approved folder.
- The "Update rule" notice (ADR 0166) is the way a stored move rule gains `drive_copy_file`.

## Verification

- `tests/unit/policy/test_registry.py` (`test_there_are_nine_verbs`,
  `test_tool_to_verb_matches_the_verb_matrix`, and the join with `TOOL_TO_GATE`).
- `tests/unit/policy/test_catalogue.py` (`test_parse_verbs_maps_legacy_names`,
  `test_group_aliases_resolve`, `test_slack_group_chats_never_reaches_send_message`,
  `test_scope_catalogue_has_no_verb_labels`).
- `tests/unit/policy/test_propose.py` (`test_gmail_drafting_scope_excludes_create_label`,
  `test_contacts_label_scope_excludes_contacts_edit`,
  `test_slack_recipient_scope_excludes_group_chat`, and `_MERGED_VERB_EXTRAS` in the catalogue
  derivation test).
- `tests/unit/policy/test_outdated.py` (a move rule without `drive.copy_file` reports it missing).
- `tests/unit/test_generate_always_allow_reference.py`.

## Related

- [`policy/registry.py`](../../src/privacyfence/policy/registry.py), [`policy/catalogue.py`](../../src/privacyfence/policy/catalogue.py), [`policy/propose.py`](../../src/privacyfence/policy/propose.py)
- ADR [0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md), ADR [0166](0166-a-stored-rule-is-widened-to-its-verbs-current-operations-only-by-an-explicit-confirmed-update.md), ADR [0170](0170-a-drive-copy-is-its-own-verb-governed-by-the-move-folder-scope.md)
