# ADR 0019: live-connector test credentials live only on a project-owned self-hosted runner

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-10 in
`docs/connector-ci-integration-plan.md`, added in `813258be` and deleted in `214a4b6d`).
Implemented: `.github/workflows/connector-live-check.yml` (`93697e93`, 2026-09-10) and
`.github/workflows/qa-record-fixture.yml` both run on the runner. The plan's choice of an
`--ephemeral` runner registration was reversed in `214a4b6d` (2026-09-11); see Decision.

## Context

Every pull request is tested against recorded connector fixtures with no credentials at all. That
catches regressions in PrivacyFence, not provider API drift (a renamed field, a newly required
scope, a moved endpoint). Detecting drift automatically needs live OAuth credentials for dedicated
test accounts at Google, Slack, Atlassian and Salesforce somewhere in CI.

Before this decision, `docs/testing-policy.md` §2 said, as a permanent position, that no connector
credential is ever provisioned to GitHub Actions or any other cloud CI. The plan kept that
position for GitHub-hosted runners and added one narrowly scoped exception.

## Decision

1. The live-connector credentials (the per-account OAuth token files and the QA
   `org_config.json`) exist only as local files on a self-hosted GitHub Actions runner that the
   project provisions and controls, labelled `privacyfence-test`. They are never stored as GitHub
   Actions secrets and never reach a GitHub-hosted runner, including on `main`-only or
   `workflow_dispatch`-only workflows.
2. Workflows that target that runner are triggered only by `schedule:` or `workflow_dispatch:`,
   never by `pull_request`, `pull_request_target` or a push from a branch a non-maintainer
   controls. The live check runs weekly, not per PR.
3. The runner never merges anything. On drift it opens an ordinary PR with the redacted fixture
   diff, which a human reviews and which then passes through the credential-free `tests.yml` gate.
4. The runner is registered **non-ephemeral**, as an always-listening service. The plan had
   specified `--ephemeral`, so that a job could not leave state for a later one. Building the
   runner showed that `--ephemeral` "fully deregisters and wipes its own config after exactly one
   job -- incompatible with a systemd-managed always-listening runner" (`214a4b6d`'s commit
   message). Per-job isolation now comes from the workflow: each run takes a clean checkout and a
   fresh venv, and only `~/privacyfence` persists between runs.

## Alternatives considered

- **Store the credentials as GitHub Actions secrets and use GitHub-hosted runners** — rejected.
  The plan: GitHub-hosted runners are "ephemeral but shared infrastructure with a documented
  history of secret-exfiltration techniques (crafted `pull_request_target` misuse, malicious
  dependency scripts reading `$GITHUB_ENV`, compromised Actions in the supply chain). A
  self-hosted runner you provision, patch, and can look at the process list of is a materially
  smaller attack surface".
- **Run live checks on every PR** — rejected: provider APIs do not drift from one PR to the next,
  and a schedule means "a compromised runner has, at most, a fixed daily/weekly window of
  credential validity to exploit rather than a live credential sitting in every PR's job logs."
- **No automation; a human runs `qa_fixture_recorder.py --check` by hand** — the plan named this as
  the other available option (no new infrastructure) and did not pick it: it depends on someone
  remembering to run the check.
- **`--ephemeral` runner registration** — the plan's own choice, reversed as described in
  Decision 4.

## Consequences

- The runner host is trusted infrastructure holding live grants. Its compromise exposes the QA
  accounts, which is why they are dedicated accounts with synthetic data, isolated from any
  production tenant.
- The plan lists what this does not protect against: compromise of the host itself, and a
  maintainer merging a malicious change to a workflow file that then runs on the runner. The
  plan's mitigations were review on changes to that workflow file and minimal job permissions.
- Drift is detected about once a week, not the moment it happens.
- Credential rotation and recovery are manual work on the runner host, as is keeping the runner
  online. The plan's risk register left "runner goes offline" unautomated: a dead runner shows as
  queued or stale runs in the Actions tab, and no workflow alerts on it.
- The rule covers per-account connector grants. The shared Telegram app id/hash, already baked
  into every release build, is read from repository secrets; `connector-live-check.yml`'s `env:`
  comment explains why that does not break the rule.
- Both workflows share one concurrency group because they write refreshed tokens back to the same
  runner-local store.

## Verification

- `.github/workflows/connector-live-check.yml`: `on:` is `schedule` + `workflow_dispatch` only;
  `runs-on: [self-hosted, privacyfence-test]`; `QA_SECRETS_DIR: ~/privacyfence`.
- `.github/workflows/qa-record-fixture.yml`: `workflow_dispatch` only, same runner and
  concurrency group; its header comment restates the trigger rule.
- `docs/testing-policy.md` §0 "Runner-local live tier" and the tier table row for
  `qa_fixture_recorder.py --check` / `--record`.
- `docs/connector-live-check-setup.md`: runner requirements, the persistent `~/privacyfence` layout,
  "Do not copy QA credentials into GitHub Actions secrets as a workaround", and "Security boundary".

## Related

- `git show 214a4b6d^:docs/connector-ci-integration-plan.md`: header, "How this differs from a
  straightforward reading of the request", B.2–B.4, and "Risk register".
- `git show -s 214a4b6d`: the commit message records the non-ephemeral reversal. The fuller
  troubleshooting text it added to `docs/connector-live-check-setup.md` was trimmed in `6de7f7cd`.
- `813258be` (plan added), `93697e93` (workflow added, testing policy updated).
