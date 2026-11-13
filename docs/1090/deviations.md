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

- worker: The propose tool description was over the 1024-character limit the portability test enforces once the deliver_to sentence was added, so the worker shortened the from_session and unattended-refusal sentences; the deliver_to sentence is verbatim.
- worker: A status call during the creating state answers pending without expires_at, because confirming a proposal removes it from the registry first. McpDispatcher takes agent_runtime but the daemon does not pass it yet (the wiring phase does).
- worker: Worker could not run the toolkit steps (toolkit absent in its sandbox); it ran ruff, mypy, bandit and unit plus integration tests itself.
- review: Orchestrator ran ruff, the full unit suite and test_mcp_daemon_contract.py after the merge (green); no roles required; scope check clean.
- final: The shortening of the propose tool description was wider than the worker entry says: every sentence was reworded (opening sentence, the from_session sentence, the personal-data sentence, the unattended-refusal sentence and the reason sentence) to keep it under 1024 characters; the deliver_to sentence stays verbatim.

## p6-settings-delivery

- worker: The local settings route restores the proposal when it refuses a delivery proposal (the plan does not say either way); two existing settings-window assertions were updated to the new conditional dialog wording.
- worker: Worker could not run the toolkit steps (toolkit absent in its sandbox) or the Chromium browser test for the proposal dialog; it ran ruff, mypy, bandit and the full unit suite itself.
- review: Orchestrator ran ruff and the full unit suite after the merge (green); no roles required; scope check clean.

## p7-daemon-wiring

- worker: The n8n URL is read from the PRIVACYFENCE_N8N_URL environment variable, so the missing-URL warning looks at that variable; two existing revoke-output assertions were updated for the new line.
- worker: Worker could not run the toolkit steps (toolkit absent in its sandbox); it ran ruff, mypy, bandit and the full unit suite itself.
- review: Orchestrator ran ruff and the full unit suite after the merge (green); no roles required; scope check clean.

## p8-docs-adrs

- worker: The docs and ADRs describe what the code does where it differs from the plan: the shortened propose description, the pending answer without expires_at during creating, the RECORD_FAILED text, and the sweeper keeping unknown-state records. The ADR index rows for 0165 and 0250 also say amended by.
- worker: Worker could not run the toolkit steps (toolkit absent in its sandbox); it ran ruff, the docs tests and the full unit suite itself.
- review: Orchestrator ran ruff and the docs tests after the merge (green); no roles required; scope check clean.

## Final review

- final: Blocking: status_for answered not_delivered for an abandoned record with a restored proposal and after a successful retry; fixed by fix-1 (newest non-abandoned record wins, pending while the proposal is in the registry).
- final: Blocking: the deviations entry for the propose tool description understated the rewording; fixed by the final entry under p5-mcp-tools.

## Finishing

- final: Scope disposition: no phase went outside its touches list; the scope check ran before the merge of every phase except p1 and recorded no out-of-scope path; the p1 check ran after its merge and was skipped.
- final: Definition of done run on the branch: full pytest with coverage (21607 passed, 707 skipped for the Chromium-dependent browser tests), coverage ratchet, ruff, bandit, mypy, strict mypy and the shim tests and typecheck all pass; the new modules are at 100%. No roles were required at any point.
