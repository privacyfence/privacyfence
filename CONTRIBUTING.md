# Contributing to PrivacyFence

## Forking

You're welcome to fork PrivacyFence and build on it. If you publish a fork or
derivative project, a brief note to the author (info@privacyfence.eu or
open a GitHub issue) is appreciated — not required, just courteous. See `NOTICE`
for details.

## Pull Requests

All changes to `main` go through pull requests. Direct pushes are blocked.

1. Fork the repo and create a branch off `main`, named `<type>/<kebab-case-description>` with
   `feature/`, `fix/`, `chore/` or `tests/` as the type. Use `feature/`, not `feat/`: a few early
   branches used `feat/`, and that prefix is retired. `plan/` branches hold a plan document while
   its work is open (see [Decisions, plans and ADRs](#decisions-plans-and-adrs)) and are never
   PR'd themselves.
2. Keep PRs focused — one logical change per PR. PRs merge with a merge commit, not a squash, so
   each commit message on your branch ends up in `main`'s history.
3. Describe *why* the change is needed, not just what it does.
4. PRs require review and approval from the maintainer (`.github/CODEOWNERS`) before merging.
5. A user-visible change adds a line under `CHANGELOG.md`'s `## [Unreleased]` heading in the same
   PR. Never open a concrete `## [X.Y.Z]` heading on a feature branch; only the PR that cuts a
   release does that — see [`docs/releasing.md`](docs/releasing.md#release-notes-come-from-changelogmd).
6. Before opening a PR, work through the checklist in
   [`docs/coding-and-testing-guidelines.md` §2.7](docs/coding-and-testing-guidelines.md#27-definition-of-done-for-a-pr-touching-this-repo)
   — it covers required test coverage and when a local QA check needs to be run and pasted into the
   PR description. See [`docs/testing-policy.md`](docs/testing-policy.md) for which checks run in CI
   versus which ones you run locally.

### Release integration branches (`releases/*`)

`releases/*` (e.g. `releases/4.1-dev`) is a long-lived, cross-cycle integration branch, cut from
`main` when a batch of work for the next release needs to accumulate somewhere other than `main`
while `main` stays frozen for a prior release's remaining blockers. It is a deliberate exception to
"feature branches go straight to `main`", not a standing convention: while one exists, branches
fork from and PR into it, with the normal `<type>/<kebab-case-description>` naming, and the
`releases/*` branch itself is deleted once it merges back into `main` in one PR.

`releases/*` is protected the same way `main` is — same ruleset (PR required, no force-push or
deletion, the same required status checks `scripts/update_branch_protection.py` manages for
`main`; run it with `--branch "releases/**"` to sync that ruleset too) and the same CI: every
`.github/workflows/*.yml` push trigger scoped to `branches: [main]` for a test, audit or lint job
also lists `"releases/**"`. This deliberately does **not** extend to the production-deploy
triggers (`pages.yml`'s website deploy, `deploy-download-worker.yml`'s Worker deploy and live
database migrations) — those stay `main`-only, so merging into a `releases/*` branch never ships to
production ahead of that branch's eventual merge into `main`.

## Decisions, plans and ADRs

Three kinds of document, three lifecycles — the full rules, template and index are in
[`docs/adr/README.md`](docs/adr/README.md):

- **Plans** (what we are about to do) are temporary: a GitHub issue, or a `docs/*-plan.md` while
  its work is open. They are deleted when the work lands.
- **ADRs** (`docs/adr/NNNN-*.md` — why it is this way, what was rejected) are permanent and frozen
  once accepted. Change your mind with a new ADR that supersedes the old one; never rewrite an
  accepted ADR's body, and never put implementation progress in one.
- **Reference docs** (`docs/*.md`, this file) describe today's behavior and link to ADRs for the
  *why* rather than retelling it.

**Retiring a plan requires extracting its decisions first.** The PR that deletes a plan document
adds or amends an ADR for every decision the plan made — anything hard to reverse, touching a trust
boundary or the release/distribution path, or rejecting an alternative for a non-obvious reason —
or says in its description that the plan made none.

The same applies when a decision is made somewhere else — a PR thread, an issue, a reference doc:
if it meets the bar above, it gets an ADR in the same PR. ADRs link to issues, PRs, commits and
source files, never to a plan document, which will not outlive it.

## Releasing

Cutting a release is a git tag, not a commit; everything else follows from the tag push. The whole
process — versioning, release notes, gating, PyPI, the R2 archive — is
[`docs/releasing.md`](docs/releasing.md).

## Issues

Use GitHub Issues for bug reports and feature requests. Include:
- Your operating system and version, and how PrivacyFence is installed (the macOS, Windows or Linux
  package, or a source checkout — then also your Python version)
- The PrivacyFence version (shown in Settings, under About)
- Steps to reproduce (for bugs)
- What connector is involved, if relevant

Do not report a security vulnerability in a public issue — follow [`SECURITY.md`](SECURITY.md).

## Code Style

- `src/privacyfence/` (the daemon): Python 3.11+, standard library preferred over new dependencies
- `mcpb/shim/` (the `.mcpb`'s stdio-to-Streamable-HTTP transport proxy, which is how Claude
  Desktop reaches the daemon): TypeScript/Node, so the `.mcpb` extension ships without a bundled
  Python runtime. It carries no tool-schema knowledge and stays small on purpose — read its
  `src/index.ts` module docstring before adding anything to it
- No comments unless the *why* is non-obvious
- Match the surrounding code's style

See [`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md) for the full,
codebase-derived conventions this project follows — coding patterns, the gate's security
invariants, how to structure and write tests for `src/privacyfence/`, and what adding a connector
involves.

## Running from source

`./scripts/dev_start.sh` runs the daemon from your checkout. A source run refuses to start on a
machine that has a packaged PrivacyFence install, so develop on a separate machine or VM — see
[`docs/dev-vs-live-setup.md`](docs/dev-vs-live-setup.md).

## License

By submitting a pull request you agree that your contribution is licensed under
the Apache License 2.0, the same license as this project.
