# ADR 0207: privacyfence-sync can start a Claude Code scheduled task, and nothing else

## Status

Accepted — 2026-10-20. Implemented. Amends [ADR 0161](0161-privacyfence-sync-is-a-separate-plain-mcp-client.md).

## Context

A user wants Claude to start on its own when a file appears in a Drive folder. An MCP server cannot
start Claude, and a Claude session that waits for hours is wasteful.

## Decision

A `trigger` step in a `privacyfence-sync` recipe starts a Claude Code cloud scheduled task through
its documented API trigger and passes a short text, such as the new file's ID.

- It sends one `POST` to the fixed URL `https://api.anthropic.com/v1/claude_code/routines/<trigger>/fire`
  and to no other address. Redirects are not followed.
- The task's token comes from an environment variable named in the step, which must start with
  `PRIVACYFENCE_`, so `script_env()` keeps it out of every script's environment. The recipe never
  holds the token.
- It never retries. A failed start leaves the commit step after it unrun, so the next scheduled
  run tries again.
- `preflight` and `permissions` never call Claude. `preflight` reports whether the variable is set.
- A refused or empty token exits 4; any other failure exits 2.

## Alternatives considered

- **A generic `http:` step that calls any URL.** It would make a recipe a general way to send data
  read through the gate anywhere. A fixed Anthropic URL and a short `text` keep the outbound
  surface to one documented endpoint. Scripts can already make any call, but they are the user's
  own reviewed code.
- **Retrying a failed start.** The endpoint has no idempotency key, so a retry can start Claude
  twice on the same file. The next scheduled run retries instead, because the commit step after the
  trigger never ran.
- **A long-waiting MCP tool, or a daemon-side watcher that starts Claude.** Tool calls time out in
  minutes, and a waiting session uses runtime the whole time. A daemon-side watcher would put an
  Anthropic credential inside the fence, and Drive push notifications need a public HTTPS endpoint
  that a local install does not have.
- **Cowork scheduled tasks.** They have no documented external trigger.
- **The token in the recipe or on the command line.** Recipes live in git, and command lines show
  in `ps`. A `PRIVACYFENCE_*` name keeps the token out of every script's environment through the
  existing `script_env()`.

## Consequences

- A recipe can now start Claude.
- `privacyfence-sync` makes one outbound call besides the daemon: ADR 0161's "reaches only the
  daemon" no longer holds for this step.
- A failed start is retried only by the next scheduled run.
- The endpoint is marked experimental and may change.

## Verification

- `sync/tests/test_trigger.py`, `sync/tests/test_recipe.py`, `sync/tests/test_runner.py`,
  `sync/tests/test_preflight.py`, `sync/tests/test_permissions.py`, `sync/tests/test_cli.py`.

## Related

- ADR [0161](0161-privacyfence-sync-is-a-separate-plain-mcp-client.md), ADR [0015](0015-unattended-session-flag-is-advisory-only.md)
- <https://platform.claude.com/docs/en/api/claude-code/routines-fire>
