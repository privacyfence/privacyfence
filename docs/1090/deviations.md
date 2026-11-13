# Deviations: agent-runtime-credentials

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-config-and-api

- worker: Worker could not run tld guard allow-plan, the dod playbook or role reviews: the toolkit plugin directory was empty in its sandbox. It ran the acceptance commands itself.
- review: Orchestrator ran ruff and the full unit suite after the merge (green) and found no scope drift; tld roles required lists no roles for this diff.

## p2-records-store

- worker: review_data also takes a project_id keyword (the delivery dict carries project_id but only runtime_host was named); a later phase must pass the configured project id.
- worker: New RECORD_FAILED text ("PrivacyFence could not write its delivery record, so it created nothing. Ask your administrator to check the server's log.") returned with status 500 when the step-2 record write fails; the plan did not say what happens then.
- worker: deliver() raises ValueError for a proposal without delivery; a set_state failure after n8n accepted the credential is handled like step 6, with the credential delete tried first; a malformed chosen token_id raises the same already-in-use text as a duplicate.
- worker: Worker could not run the toolkit steps (toolkit absent in its sandbox); it ran ruff, mypy, bandit and the full unit suite itself.
- review: Orchestrator ran ruff and the full unit suite after the merge (green); no roles required; scope check clean.

## p3-sweeper

- worker: Where a token state is unknown (unreadable token file), the sweeper skips and keeps the record under rules 1 and 5 as well as rule 3, and keeps it when the revoke in rule 1 raises; the plan did not cover this, and the choice never deletes a credential while a token may be live.
- worker: Worker could not run the toolkit steps (toolkit absent in its sandbox); it ran ruff, mypy, bandit and the full unit suite itself.
- review: Orchestrator ran ruff and the full unit suite after the merge (green); no roles required; scope check clean.

## p4-admin

- worker: Worker could not run the toolkit steps (guard, dod playbook, roles): the toolkit was absent in its sandbox. It ran the acceptance commands, ruff, mypy and bandit itself.
- retry: Sent back once for a missing phase trailer that was in fact present; the worker added an empty marker commit.
- review: Orchestrator ran ruff and the full unit suite after the merge (green); no roles required; scope check clean.

## p5-mcp-tools

- pending

## p6-settings-delivery

- pending

## p7-daemon-wiring

- pending

## p8-docs-adrs

- pending

## Final review

- pending

## Finishing

- pending
