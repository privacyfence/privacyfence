# AGENTS.md

For agents working on PrivacyFence from a GitHub issue. Humans and Claude Code sessions: see
[`CLAUDE.md`](CLAUDE.md). This file only adds what a cold, offline, no-browser agent needs; the rules
themselves live in [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md) (definition of done: §2.7).

## Repo map

- `src/privacyfence/`: the Python daemon. `gate.py` and `audit_log.py` are the policy core.
  `connectors/` has one module per service (the `*_client.py` files beside it are the API clients).
  `web/` is the embedded approval/settings UI and the MCP endpoint.
- `tests/unit/` (fast, offline), `tests/integration/`, `tests/platform/`, `tests/system/`.
- `website/` is the public Jekyll-style site; `docs/` the reference docs; `docs/adr/` the decisions.
- `cloudflare/` (Workers, npm), `mcpb/` (Node stdio shim for Claude Desktop), `sync/` (separate Python
  package with its own tests), `scripts/`, `requirements/*.lock.txt` (generated, never hand-edit).

## Verify

The check command is the `verify_command` in `.agents/config.toml`: `ruff check .` then
`pytest tests/unit -q`, offline. To rerun just your part, use the same interpreter and `pytest <path> -q`.

It does **not** cover: Playwright/browser tests (they skip), `npm test`/`typecheck` in `mcpb/shim` and
`cloudflare/*`, `sync/` tests, `tests/integration`, bandit, mypy, the 100% coverage floor, live connector
QA. CI runs those on the PR; do not claim them as passed. Say so in the PR description.

## Definition of done (condensed from §2.7)

- User-visible change: one line under `## [Unreleased]` in `CHANGELOG.md`, never under a version heading.
- Hard to reverse, trust boundary, build/release path, or a rejected alternative: an ADR in `docs/adr/`.
- Every new or changed tool call goes through `gated_call` (or an explicit always-auto-approve
  connector) and leaves an audit trail.
- No preview dict carries full content; no log line carries a credential or a message/document body.
- New client code has a `<Name>ClientError`; connector code catches it and re-raises `RuntimeError`.
- New module-level state gets a reset in `tests/conftest.py`.
- Comments only for the non-obvious *why*. Every module keeps its docstring.

## Never touch

`.github/workflows/`, `.agents/`, anything about releases (tags, `docs/releasing.md` procedure,
version headings in `CHANGELOG.md`), and `credentials/`. Do not add credentials, tokens or real account
data anywhere, including tests and fixtures.

## Connectors are two PRs

A new connector is connect, then tools, with human QA on the browser connection between them
([guide](docs/coding-and-testing-guidelines.md#3-adding-a-connector)). Do exactly ONE of the two per task,
whichever the issue asks for, and stop. You cannot do the QA: no browser, no real accounts, and no
dispatching workflows; leave the QA row of the PR template for a human.
