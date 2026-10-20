# ADR 0161: privacyfence-sync is a separate plain MCP client

## Status

Accepted — 2026-10-11. Implemented.
Amended by [ADR 0207](0207-privacyfence-sync-can-start-a-claude-code-scheduled-task-and-nothing-else.md).

## Context

Refreshing a data lake and a dashboard is a regular, deterministic job that should cost no model
tokens. Claude designs the sync; something else must repeat it. That something must work against a
local daemon and against an organization server, where the user has no PrivacyFence install.

## Decision

`privacyfence-sync` is a separate distribution in `sync/`, published to PyPI as its own project by
its own jobs after the main package's, and a plain MCP client of `/mcp`.

- It imports nothing from `privacyfence`; it reaches the daemon only through tool calls, the
  capability URLs they return, and the OAuth endpoints. A test walks the source with `ast`.
- Script steps run on the user's side. They get no PrivacyFence credential in their environment and
  receive everything as files.
- Files move only through upload slots and download links, which work in every mode.
- A recipe has no branching beyond a script stopping the run successfully.
- It declares an unattended session, so a step no rule covers fails at once. It cannot approve
  anything or change rules.

## Alternatives considered

- **Running the transform inside the daemon, in a sandbox.** The daemon holds the credentials under
  its own account so that nothing an AI writes executes inside the fence (ADR 0003); Apps Script got
  no run tool for the same reason (ADR 0075). The exported file is already on the user's side once
  its export was approved, so transforming it there adds no exposure.
- **A daemon-side source-to-destination pipe.** A flow that moved data between connectors with no
  gate decision would bypass the approval model the product exists for.
- **Shipping the CLI inside the `privacyfence` wheel.** In org mode the user has no local
  PrivacyFence install; the client must install alone, without the daemon's dependencies.
- **`local_path` for uploads and `destination_dir` delivery only.** A privilege-separated daemon
  cannot read or write the user's files, and an org daemon is another machine; upload slots and
  download links work in every mode.

## Consequences

- Every call goes through the gate, rules and audit log exactly as Claude's do.
- A second PyPI project needs its own trusted publisher; a missing one cannot block the main upload.
- A scheduled org-mode run needs `privacyfence-sync login` again once a month (refresh tokens are
  capped at 30 days).

## Verification

- `sync/tests/test_no_daemon_imports.py`, `sync/tests/test_runner.py`, `sync/tests/test_cli.py`,
  `sync/tests/test_recipe.py`, `sync/tests/test_auth.py` (100% coverage, the `sync` job in
  `.github/workflows/tests.yml`).
- `.github/workflows/publish-pypi.yml` (`publish-testpypi-sync`, `publish-pypi-sync`),
  `tests/unit/test_update_branch_protection.py`.

## Related

- [`sync/`](../../sync/), [`sync/README.md`](../../sync/README.md)
- ADR [0003](0003-separated-installs-only.md), ADR [0015](0015-unattended-session-flag-is-advisory-only.md), ADR [0028](0028-clients-without-the-shim-get-capability-urls.md), ADR [0075](0075-apps-script-gets-no-run-tool.md), ADR [0155](0155-exports-write-a-file-and-return-only-counts-and-a-hash.md)
