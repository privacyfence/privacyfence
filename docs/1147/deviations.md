# Deviations: drive-property-tag-rules

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-client-and-tags

- None.

## p2-tag-scope

- worker: No docs.* read operation exists (a Doc is read via drive.read_file_contents), so the READ test asserts drive.read_file_contents and sheets.read_values and a separate UPDATE test asserts the docs.* operations.
- scope: scope: tests/unit/policy/test_scope_rows.py (added a drive.tag entry to SAMPLE_VALUES; a test there counts catalogue entries; also in touches of p4 and p5)
- review: The new drive.tag entry in NEW_SCOPE_SELECTORS sits between the existing gmail.query comment and its entry, so that comment now sits above drive.tag; non-blocking, left for the final review.

## p3-exempt-operations

- None.

## p4-set-file-tags-tool

- worker: The tool description says "Requires user approval every time, which no rule can grant" instead of "Always requires user approval", because test_every_tool_definition_is_complete needs the exact phrase "Requires user approval".
- worker: Regenerated docs/tools-reference.md and docs/always-allow-rules-reference.md with the repo generator scripts and bumped the website tool counts (171 to 172 tools, Drive 26 to 27, popup 14 to 15), because pinned tests fail otherwise; the hand-written p7 docs work is untouched. Also added drive_set_file_tags arg_overrides to the audit-trail test and raised the _VERB_MATRIX count 112 to 113 in test_registry.py.
- scope: scope: docs/always-allow-rules-reference.md (generated or pinned count update; also in touches of p7-retire)
- scope: scope: docs/tools-reference.md (generated or pinned count update; also in touches of p7-retire)
- scope: scope: website/connectors/index.html (generated or pinned count update; also in touches of p7-retire)
- scope: scope: website/how-it-works/index.html (generated or pinned count update; also in touches of p7-retire)
- review: Defect found in p3: propose.proposals_for (policy/propose.py) does not skip RULE_EXEMPT_OPERATIONS, so the approval popup and the generated always-allow reference offer an Always-allow button for drive_set_file_tags although the engine never lets a rule cover it. Plan 3.5 does not name proposals_for. Left for the final review to settle; the fix is to skip exempt operations there, regenerate docs/always-allow-rules-reference.md and add a test.

## p5-tag-folder-tree-tool

- worker: The tool description says "Requires user approval every time, which no rule can grant" instead of "Always requires user approval", because test_every_tool_definition_is_complete pins the wording (as in p4).
- worker: Regenerated docs/tools-reference.md and docs/always-allow-rules-reference.md and bumped website tool counts (173 tools, Drive 28 tools, 16 need approval) because pinned tests fail otherwise; raised the _VERB_MATRIX count 113 to 114 in test_registry.py.
- scope: scope: docs/always-allow-rules-reference.md (generated or pinned count update; also in touches of p7-retire)
- scope: scope: docs/tools-reference.md (generated or pinned count update; also in touches of p7-retire)
- scope: scope: website/connectors/index.html (generated or pinned count update; also in touches of p7-retire)
- scope: scope: website/how-it-works/index.html (generated or pinned count update; also in touches of p7-retire)

## p6-recorder-check

- None.

## p7-retire

- worker: Edited scripts/generate_always_allow_reference.py (outside touches) to add the "Google Drive - files with a tag" section to the generator footer, because a test compares the generated always-allow reference with a fresh run; then reran the generator.
- scope: scope: scripts/generate_always_allow_reference.py (generator footer for the new scope section; no other phase touches it)

## Final review

- pending

## Finishing

- pending
