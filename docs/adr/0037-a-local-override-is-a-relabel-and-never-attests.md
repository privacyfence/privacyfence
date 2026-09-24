# ADR 0037: a local override is a relabel and never attests

## Status

Accepted — 2026-09-24, decided by the maintainer in review of #655.
Amends [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) option D and the
`override` row of its decision 2, for local mode.

## Context

[ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) option D gives local mode
an override: a human-written mapping in `settings.yaml` from a name to a registry AI system,
ranked first and recorded as `agent_source: "override"`, an attested source. ADR 0006 argued that
on a separated install the file is under the service account and out of the agent's reach, which
"makes a local override genuinely stronger than `clientInfo`".

#655 first implemented exactly that: `agent_overrides:` was recorded as `override` when the
install was privilege-separated and the loaded file resolved inside the service-owned
`authority_dir()`, and as `client_info` anywhere else.

Review found that this still turns a caller-supplied signal into an attested source. The file is
out of the agent's reach, but the file is not what selects the mapping. The **selector** is the
`clientInfo.name` the caller sends in its own handshake. And local mode has one MCP token per OS
user ([ADR 0008](0008-one-principal-per-os-user.md)), shared by every AI system that user runs.
So with `python-mcp-client: claude-code` in a service-owned `settings.yaml`, any process holding
that token can send `python-mcp-client` and be recorded as attested Claude Code. What the file
attests is that a human mapped a *name*. Nothing on the request attests which process sent it.
That violates ADR 0006 Verification 2 and ADR 0035: no step converts a caller-supplied signal into
an attested `agent_source`.

## Decision

- **An `agent_overrides:` match is always recorded as `client_info`**, on every install —
  privilege-separated or not, whichever file it was read from. It relabels the name: the call is
  named after the registry entry the human chose, and shown as *Not verified*.
  `agent_overrides.AgentOverrides.resolve()` has no attested path.
- **Local mode has no attested source.** `AgentSource.OVERRIDE` stays in the vocabulary, unused,
  for the per-credential override below.
- **Org mode is unchanged.** An administrator's pin of an OAuth `client_id`
  ([ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
  decision 3) records `oauth_client` and remains the only attested source. It is keyed by the
  access token's `client_id`, which the authorization server issued, not by anything the caller
  says.
- **Ranking, in `web/routes_mcp.py`'s `_resolve_agent`:**
  1. an org admin pin (`oauth_client`);
  2. a local override relabel (`client_info`);
  3. the DCR `client_name` (`client_info`);
  4. the handshake name (`client_info`).
- **The section is read once at startup**, like the rest of `settings.yaml`'s web wiring. An
  entry naming an unknown registry id is skipped with a warning.

## Alternatives considered

- **Attest an override on a separated install reading the service-owned file** (what #655 first
  shipped). Rejected for the spoof above: the file being out of the agent's reach proves who
  wrote the mapping, not who sent the name that selects it. With one shared local token, any AI
  system on the machine can send any mapped name and be recorded as attested. No outcome keys on
  `agent_id` today, but the attested tier is the one ADR 0006 decision 3 lets a future rule key
  on, so an attested value any caller can produce would be a bypass waiting for its first rule.
- **Drop the section.** The relabel is still useful — it turns an unhelpful name such as
  `python-mcp-client` into the AI system the human knows it is — and it is harmless, because a
  claimed identity changes no outcome.

## Consequences

- One `settings.yaml` gives the same `agent_source` everywhere; the audit log never records a
  local call as verified.
- Nothing a user can configure in local mode makes a call *Verified*. The Audit Log's tier marker
  and the approval card's brand mark only ever appear verified in org mode, for a pinned client.

## What would change this

A credential that identifies the client by itself: ADR 0006 option D's "per-credential" override,
with a separate local MCP token per AI system. A mapping keyed by *which token* authenticated the
call, rather than by the name the call claims, is not caller-supplied, and could record
`override` again. That needs per-credential local tokens, which do not exist yet; it would be a
new ADR superseding this one.

## Verification

- `src/privacyfence/agent_overrides.py` (`AgentOverrides.resolve`, `from_config`),
  `src/privacyfence/web/routes_mcp.py` (`_resolve_agent`), `src/privacyfence/daemon_main.py`
  (`_maybe_start_web_server`).
- `tests/unit/web/test_agent_attestation.py`, `TestLocalOverrideNeverAttests`: a match is
  `client_info` on a separated and an unseparated install, with the config inside
  `authority_dir()`, elsewhere, or unnamed, through the daemon's own startup wiring; the spoofed
  `python-mcp-client` case; a pin outranks an override.
- `tests/unit/web/test_routes_mcp_agent_identity.py`, `TestNoCallerSignalIsAttested.test_local_override`:
  ADR 0006 Verification 2 over the live `/mcp` endpoint, with every spoofed name mapped.

## Related

- [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) option D and decisions
  2 and 3, [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md),
  [ADR 0003](0003-separated-installs-only.md), [ADR 0008](0008-one-principal-per-os-user.md).
- #655.
- Issues #579, #580.
