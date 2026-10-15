# ADR 0181: the QA runner is reachable only from `main`'s two QA workflows, and its jobs hold read-only tokens

## Status

Accepted — 2026-10-15. Implemented in the workflows; the runner-group settings are made on GitHub.
Amends [ADR 0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md).

## Context

[ADR 0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md) Decision 2 keeps the
live QA grants safe by convention: only the two workflow files that target the self-hosted runner
may do so, and neither runs on `pull_request`. Nothing enforced that. The runner was registered
to the repository, so any workflow a pull request added could name its label, and a job on it
could read the credentials on disk. The two QA workflows also held `contents: write` (and, for
the live check, `pull-requests: write`) on the runner itself, and `qa-record-fixture.yml` was
dispatched against a feature branch and pushed back to it, so the code that ran next to the grants
was the branch's.

The organization is on GitHub Team, so organization runner groups with a "Selected workflows"
restriction are available.

## Decision

1. **The runner sits in the organization runner group `privacyfence-test`**, which allows public
   repositories and is restricted to `connector-live-check.yml` and `qa-record-fixture.yml` on
   `main`. The repository-level registration is removed. Fork pull requests from outside
   collaborators need approval before any workflow runs.
2. **`qa-record-fixture.yml` is dispatched on `main`** with a required `branch` input naming the
   branch to record for. The `record` job takes the input only through `env:`, refuses to run
   unless `github.ref` is `refs/heads/main`, refuses `main`, `stable`, `releases/*`, `refs/…`
   and `pull/…` names and any name `git ls-remote --heads` does not find, and only then checks the
   branch out.
3. **Jobs on the runner hold `contents: read` only.** They upload what they produced as an
   artifact. A GitHub-hosted job (`commit-fixture` in the recording workflow, `open-drift-pr` in the
   live check) downloads it and does the writing: the commit, the push, the drift pull request.

## Alternatives considered

- **Keep the repository-level runner and rely on the two workflows.** Rejected: that is the
  convention that failed to hold; a pull request adds a workflow and targets the label.
- **An ephemeral, per-job runner.** Rejected for the reason ADR 0019 already gives: the credentials
  would have to be provisioned for every job.
- **Dispatch the recording workflow against the feature branch, as before.** Rejected: a runner
  group can name a workflow file only on `main`, so the branch's copy of the file would not be
  allowed to run.

## Consequences

- A pull request, including one from a fork, cannot reach the runner or its credentials.
- Recording a fixture takes one extra hosted job, and the dispatch names the branch twice (the
  ref is `main`, the `branch` input is the branch).
- The group and fork-approval settings live outside the repository; they are documented in
  [`docs/connector-qa.md`](../connector-qa.md) and a maintainer must keep them.

## Verification

- `.github/workflows/connector-live-check.yml` and `.github/workflows/qa-record-fixture.yml`
  (`permissions:`, the `env:` input, the refusal step, the artifact hand-off).
- [`docs/connector-qa.md`](../connector-qa.md) "Runner requirements" and "Recording one connector".

## Related

- [ADR 0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md)
