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

- pending

## p4-set-file-tags-tool

- pending

## p5-tag-folder-tree-tool

- pending

## p6-recorder-check

- pending

## p7-retire

- pending

## Final review

- pending

## Finishing

- pending
