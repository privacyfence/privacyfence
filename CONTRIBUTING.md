# Contributing to PrivacyFence

## Forking

You're welcome to fork PrivacyFence and build on it. If you publish a fork or
derivative project, a brief note to the author (info@privacyfence.eu or
open a GitHub issue) is appreciated — not required, just courteous. See `NOTICE`
for details.

## Pull Requests

All changes to `main` go through pull requests. Direct pushes are blocked.

1. Fork the repo and create a branch off `main`, named `<type>/<kebab-case-description>` with
   `feature/`, `fix/`, `chore/` or `tests/` as the type.
2. Keep PRs focused — one logical change per PR. PRs merge with a merge commit, not a squash, so
   each commit message on your branch ends up in `main`'s history.
3. Describe *why* the change is needed, not just what it does.
4. PRs require review and approval from the maintainer (`.github/CODEOWNERS`) before merging.
5. Before opening a PR, work through the checklist in
   [`docs/coding-and-testing-guidelines.md` §2.7](docs/coding-and-testing-guidelines.md#27-definition-of-done-for-a-pr-touching-this-repo)
   — it covers required test coverage and when a local QA check needs to be run and pasted into the
   PR description. See [`docs/testing-policy.md`](docs/testing-policy.md) for which checks run in CI
   versus which ones you run locally.

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
