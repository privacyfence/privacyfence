# ADR 0225: The admin AI-system pin is removed

## Status

Accepted — 2026-10-22. Decided by the maintainer in planning. Implemented: `AgentPinStore`, the pin actions and the **AI systems** settings page are gone.
Supersedes [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md) decision 3.

## Context

[ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
decision 3 let an organization administrator pin a client registration to an AI system on
**Settings → AI systems**. A pin changed only the card's badge and icon; no decision keys on it. It
had to be repeated for every member and every re-registration, and it asked the administrator to
check something they could not check: a registration's name is whatever the client sent.
[ADR 0224](0224-an-org-registration-is-attested-by-its-redirect-uris.md) attests the registrations
that can be attested, by their redirect URIs, with no administrator step.

## Decision

The per-registration pin, the **AI systems** settings page and the two actions behind it
(`pin_agent_client`, `unpin_agent_client`) are removed. Pins are no longer read. An existing
`org/agent_pins.json` is left on disk unread and may be deleted. Nothing migrates pins: a pinned
claude.ai, ChatGPT or Gemini Enterprise registration is attested by its redirect URI anyway, and a
pinned Claude Code registration goes back to the claimed tier.

## Alternatives considered

- **Pinning by name** ("trust every registration called Claude") — reopens the DCR spoofing bypass
  ADR 0035 decision 3 was written against.
- **Keeping the pin for loopback clients** — an administrator cannot verify those either, since any
  local program can register a loopback redirect.

## Consequences

The org-mode settings nav is General, Auto-accept, Privacy Filter, Audit Log, Stats, About. No
ORG_MODE-only settings action remains. A client that is not attested by ADR 0224 stays Not verified
and no administrator action changes that.

## Verification

- `tests/unit/web/test_org_settings_scope.py::test_there_are_no_org_only_actions`.
- `git grep "pin_agent_client\|AgentPinStore" -- src tests` prints nothing.

## Related

- [ADR 0224](0224-an-org-registration-is-attested-by-its-redirect-uris.md), [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
