# ADR 0075: The Apps Script connector has no tool that runs a script

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-08-05 in
[#154](https://github.com/privacyfence/privacyfence/issues/154)'s "Non-goals", implemented in
`ea5a0c9b` and merged in [#171](https://github.com/privacyfence/privacyfence/pull/171)).
Implemented.

## Context

The workflow behind the Apps Script connector: Claude writes or revises a Google Apps Script, the
user runs it, and Claude helps read the result. Three parts of that workflow fit PrivacyFence's
review model, because each one shows the human the actual object being touched: reading the
script's source, writing new source, and reading the result of a run. Running the script does not
fit it.

Every other gated tool shows the approver the real content or object it reads or changes: the
email, the file, the rows. Once an Apps Script starts on Google's servers, its runtime is opaque to
PrivacyFence. There is no per-call view of the Drive, Gmail or other APIs it goes on to call, so the
gate has nothing to show except the script's name. A "run this script" approval would therefore
approve whatever the script does, which is a blank cheque rather than a reviewed decision.

## Decision

Running a script is out of scope for PrivacyFence. `connectors/apps_script.py` exposes exactly four
tools: `apps_script_list_projects` (auto, metadata only), `apps_script_get_content` and
`apps_script_get_execution_log` (review-gated reads), and `apps_script_write_content` (popup-gated
write). `apps_script_client.py` has no `run` method, and its OAuth `SCOPES` are only
`script.projects`, `script.processes` and `drive.metadata.readonly`, none of which runs a script.

The user runs a script in the Apps Script editor or through its own triggers, under their own
Google account and through Apps Script's own consent screen, without PrivacyFence.
`apps_script_get_execution_log` reads back what that run reported.

A run or execute tool must not be added without a new threat-model discussion, in its own issue,
that answers the blank-cheque problem above. A proposal that does so should be recorded as a new
ADR that supersedes this one.

## Alternatives considered

- **A popup-gated run tool.** It would look like every other gated write, but the popup could show
  only the script's identity, not what the run will read, change or send. Approving it would release
  the script's whole effect unseen. It would also need the script bound to a standard GCP project,
  and the API caller pre-granted every scope the script's code uses, which is setup friction with no
  governance benefit in return.
- **Gate the run by showing the source.** The current source is already reviewable through
  `apps_script_get_content`, but the source does not show which items a run will touch. That depends
  on runtime data, and the gate cannot inspect it as it happens.

## Consequences

- Claude cannot close the loop by itself. A person has to start each run, outside PrivacyFence.
- The execution log is a summary (status, duration and function name per run, from
  `listScriptProcesses`), not a `console.log` transcript, because a transcript would need
  per-script Cloud Logging setup.
- Code that Claude wrote runs with the user's Google permissions only after the user has chosen to
  run it. Reviewing the write (`apps_script_write_content`) is the checkpoint PrivacyFence does
  control.

## Verification

- `src/privacyfence/connectors/apps_script.py`'s tool list and `src/privacyfence/apps_script_client.py`'s
  `SCOPES` contain no execute path.
- `src/privacyfence/auto_accept.py`'s `TOOL_TO_GATE` lists no Apps Script tool beyond the four
  above.

## Related

- [#154](https://github.com/privacyfence/privacyfence/issues/154): the scoping issue and its
  non-goals.
