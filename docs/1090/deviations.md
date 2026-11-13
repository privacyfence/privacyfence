# Deviations: agent-runtime-credentials

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-config-and-api

- worker: Worker could not run tld guard allow-plan, the dod playbook or role reviews: the toolkit plugin directory was empty in its sandbox. It ran the acceptance commands itself.
- review: Orchestrator ran ruff and the full unit suite after the merge (green) and found no scope drift; tld roles required lists no roles for this diff.

## p2-records-store

- pending

## p3-sweeper

- pending

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
