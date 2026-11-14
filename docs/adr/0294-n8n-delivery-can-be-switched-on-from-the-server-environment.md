# ADR 0294: n8n delivery can be switched on from the server environment

## Status

Accepted — 2026-11-14. Implemented.
Amends [ADR 0253](0253-ai-token-proposals-are-off-in-organization-mode-until-the-administrator-turns-them-on.md), [ADR 0279](0279-the-n8n-address-and-api-key-come-only-from-the-server-environment.md) and [ADR 0282](0282-runtime-delivery-is-organization-mode-only-and-starts-from-an-ai-proposal.md); relates to [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md) and [ADR 0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md).

## Context

An operator of the Docker deployment configures the server through `privacyfence.env`. Today only the n8n
address and API key come from there ([ADR 0279](0279-the-n8n-address-and-api-key-come-only-from-the-server-environment.md)).
The on/off switch, the n8n project id and the PrivacyFence MCP address are set only on Admin → Agent runtimes, and
delivery also needs scoped tokens and AI proposals turned on under Admin → Scoped tokens
([ADR 0282](0282-runtime-delivery-is-organization-mode-only-and-starts-from-an-ai-proposal.md)). An operator who
wants a server that delivers tokens to n8n from the first start has to open Admin. Peek and Grist already have an
environment switch that wins and is locked on Admin (`PRIVACYFENCE_PEEK_AUTO_PROVISION`).

## Decision

Three variables, read on every call and only in organization mode (elsewhere they are ignored with the warning
"is ignored outside organization mode"). Unset or empty means the Admin page decides, as before. Values are stripped.

| Variable | Accepts | Effect while set |
|---|---|---|
| `PRIVACYFENCE_N8N_DELIVERY` | `true`, or unset | Locks `agent_runtimes.n8n.enabled`, `scoped_tokens.enabled` and `scoped_tokens.allow_ai_proposals` to true. `scoped_tokens.max_days` stays editable. |
| `PRIVACYFENCE_N8N_PROJECT_ID` | 1 to 36 letters and digits | Locks `agent_runtimes.n8n.project_id`. |
| `PRIVACYFENCE_N8N_MCP_URL` | `https://`, or `http://` on localhost | Locks `agent_runtimes.n8n.mcp_url`. |

- Any other value refuses the start. `PRIVACYFENCE_N8N_DELIVERY=true` without the n8n address and API key also
  refuses the start, as Peek's rule does.
- **A set variable wins and is locked.** `oauth_apps.effective_org_config` applies the values to the effective
  config in organization mode, so every reader (the daemon, the web server, the scoped-token policy) sees them.
  They are never written to the admin config, so removing a variable returns the field to what Admin stored.
- Admin → Agent runtimes and Scoped tokens show a locked field with the variable's value, disabled, and name the
  variable. A save that posts a different value for a locked field is refused; one that repeats it changes nothing
  stored.
- `PRIVACYFENCE_N8N_DELIVERY=true` is the operator opting in that [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md) and [ADR 0253](0253-ai-token-proposals-are-off-in-organization-mode-until-the-administrator-turns-them-on.md) ask the administrator
  to make, and it is the same file-and-environment authority that already holds the n8n address and key.

## Alternatives considered

- **Seeding the Admin values on the first start only.** Admin would then own the value, and the owner chose lock.
- **A separate scoped-tokens variable.** The owner chose that `PRIVACYFENCE_N8N_DELIVERY=true` implies it.
- **Accepting `false`.** An explicit off would have to override Admin, and nothing needs it.

## Consequences

- While `PRIVACYFENCE_N8N_DELIVERY=true` is set, Admin cannot turn scoped tokens or AI proposals off.
- Unsetting the variable returns both to what Admin stored.
- A variable set to `true` without the n8n address and key refuses the start, where an Admin-enabled switch only warns.

## Verification

- `tests/unit/test_agent_runtimes.py`: `TestEnvironmentLocks`, `TestApplyEnvironment`, `TestCheckEnvironment`, `TestSetEnvironmentVariables`.
- `tests/unit/test_oauth_apps.py`: `TestN8nEnvironmentOverlay`.
- `tests/unit/test_admin_config.py`: `TestAgentRuntimes`, `TestScopedTokensLocked`; `tests/unit/web/test_routes_admin.py`: `TestAgentRuntimes`.
- `tests/unit/test_settings_window_html.py`: `TestAdminEnvironmentLocks`; `tests/unit/test_daemon_main.py`: `TestOrgModeConnectorRegistry`.

## Related

- Issue: [issue 1105](https://github.com/privacyfence/privacyfence/issues/1105); sources `src/privacyfence/agent_runtimes.py`, `src/privacyfence/oauth_apps.py`, `src/privacyfence/admin_config.py`.
- [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md), [ADR 0253](0253-ai-token-proposals-are-off-in-organization-mode-until-the-administrator-turns-them-on.md),
  [ADR 0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md), [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md),
  [ADR 0279](0279-the-n8n-address-and-api-key-come-only-from-the-server-environment.md), [ADR 0282](0282-runtime-delivery-is-organization-mode-only-and-starts-from-an-ai-proposal.md).
