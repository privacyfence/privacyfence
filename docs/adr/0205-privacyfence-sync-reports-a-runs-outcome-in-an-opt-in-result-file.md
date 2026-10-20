# ADR 0205: `privacyfence-sync` reports a run's outcome in an opt-in result file

## Status

Accepted — 2026-10-20. Implemented: `sync/src/privacyfence_sync/result.py` and `cli.py`.

## Context

A recipe's script step can exit 10 to say "nothing changed, stop here". `run` then exits 0, the
same as a full run, so a scheduler cannot tell a quiet day from a run that moved data. The exit
codes (1 recipe, 2 failure, 3 denied, 4 not signed in) are already what every cron or launchd
wrapper tests.

## Decision

`privacyfence-sync run --result-file PATH` writes one JSON file for every outcome `main` maps to
an exit code (0 to 4): `version`, `recipe`, `outcome`, `exit_code`, `stopped_at_step`, `started_at`,
`finished_at`, `privacyfence_sync_version`. `outcome` is `ok`, `no_change`, `recipe_error`, `failed`,
`denied` or `not_signed_in`.

- Exit codes are unchanged; `no_change` still exits 0.
- The option is opt-in. Without it nothing is written.
- The file holds no error text: a script's stderr can hold data. The log keeps the text.
- It is written to a `.tmp` sibling and renamed. If that fails, the command says so on stderr and
  keeps the run's own exit code.
- A usage error, an uncaught exception or an interrupt writes no file, so a scheduler treats a
  missing file as a failure.

## Alternatives considered

- **Exit 10 for "no change".** Every existing wrapper treats a non-zero exit as a failure, so quiet
  days would become alerts. A file the wrapper has to ask for breaks nothing.

## Consequences

- A scheduler can show "no change" as such, and can tell which step stopped the run.
- The CLI gains one more contract to keep stable: the file carries `version: 1`, so a change to a
  field's meaning needs a new version.
- `run_recipe` returns the stopping step's id (or `None`) instead of a boolean.

## Verification

`sync/tests/test_result.py` (outcomes, field formats, atomic write); `sync/tests/test_cli.py`
(`ok`, `no_change`, `denied`, `recipe_error`, an unwritable path); `sync/tests/test_runner.py`
(the return value). The sync package keeps 100% coverage.

## Related

[ADR 0161](0161-privacyfence-sync-is-a-separate-plain-mcp-client.md).
Source plan: `git show 7251c292^:docs/sync-scheduling-support-plan.md`.
