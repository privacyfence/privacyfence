# ADR 0279: The n8n address and API key come only from the server environment

## Status

Accepted — 2026-11-13. Implemented.
Relates to [ADR 0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md) and [ADR 0241](0241-privacyfence-provisions-peek-accounts-with-a-scoped-key-and-only-calls-peek.md).

## Context

The n8n API key can create and delete credentials. Wherever PrivacyFence sends it is where an attacker who takes
over an administrator's browser session would send it too, if the address were editable on the Admin page.
Peek and Grist solve this by reading their addresses and keys from the environment
(`src/privacyfence/admin_config.py`, [ADR 0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md)).

## Decision

- `PRIVACYFENCE_N8N_URL` and `PRIVACYFENCE_N8N_API_KEY` (or `PRIVACYFENCE_N8N_API_KEY_FILE`) come only from the
  server's environment. The key is read per use with `org_mode.provisioning_key`, and is never written to a file
  PrivacyFence owns, a log line, an audit entry, an error text or a response.
- The address must be `https`, on any host except an IP literal that is link-local, unspecified or multicast and the
  host `metadata.google.internal`; userinfo, a query and a fragment are refused. Private hosts are allowed, because
  n8n is usually internal. A malformed address refuses start-up.
- **Admin → Agent runtimes** edits only `agent_runtimes.n8n.enabled`, `project_id` and `mcp_url` (action
  `set_agent_runtimes`, sensitive, restart, organization only). It shows the address and whether the key is set,
  read-only, with the two variable names.
- A key set both inline and as `_FILE`, or an unreadable key file, refuses start-up. A missing address or key only
  makes delivery unavailable, with one warning.
- **Departure from ADR 0241.** ADR 0241 provisions Peek "with a scoped key, not an admin key". n8n's API-key scopes
  are Enterprise-only; on other editions the key has full access. The documentation tells the administrator to give an
  Enterprise key only `credential:create` and `credential:delete`, and accepts the full-access key elsewhere.

## Alternatives considered

- **Editing the n8n address on the Admin page and clearing the key's variable on a move.** The variable has a
  default, so clearing it resolves to the same key; the address comes from the environment instead.
- **Storing the key in the admin config.** A file PrivacyFence owns would then hold a credential that can create
  credentials in n8n.

## Consequences

- Changing the n8n address needs the server's environment and a restart.
- On a non-Enterprise n8n, a leaked key grants full API access to that n8n user.

## Verification

- `tests/unit/test_agent_runtimes.py`: `TestFromOrgConfig`, `TestNormalizeUrl`, `TestApiKey`, `TestApiFor`.
- `tests/unit/test_admin_config.py`: `TestAgentRuntimes`; `tests/unit/web/test_routes_admin.py`: `TestAgentRuntimes`
  (a base-url or key field is refused, the state never carries the key).
- `tests/unit/test_daemon_main.py`: `TestOrgModeConnectorRegistry` (a malformed section, or a key set twice, refuses start-up).

## Related

- Issue: [issue 1090](https://github.com/privacyfence/privacyfence/issues/1090); sources `src/privacyfence/agent_runtimes.py`, `src/privacyfence/web/routes_admin.py`.
- [ADR 0241](0241-privacyfence-provisions-peek-accounts-with-a-scoped-key-and-only-calls-peek.md),
  [ADR 0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md), [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md).
