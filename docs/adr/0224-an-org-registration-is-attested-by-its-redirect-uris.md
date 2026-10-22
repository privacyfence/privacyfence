# ADR 0224: In organization mode a registration is attested by its redirect URIs

## Status

Accepted — 2026-10-22. Decided by the maintainer in planning. Implemented in `agent_identity.py` (`entry_for_redirect_uris`) and `OrgOAuthProvider.attested_agent_id`.
Amends [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) and [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md) (decision 3).

## Context

[ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
decision 3 made an administrator's pin of a registration the only attested organization-mode
signal. That signal is cosmetic today: no decision keys on it, only the approval card's icon and
badge (`approval_window_html.py`, `approval_list_html.py`). It is per registration, so it had to be
repeated for every member and every re-registration, and the administrator could not check what
they were pinning: a registration's name is whatever the client sent at registration.

The one fact about a registration that is neither chosen after the fact nor checkable only by the
admin is where its authorization code can go. The MCP SDK refuses an `/authorize` whose
`redirect_uri` is not in the registration's `redirect_uris`
(`mcp/shared/auth.py`, `validate_redirect_uri`) and a `/token` whose `redirect_uri` differs from the
one the code was issued for. PrivacyFence issues tokens only through the authorization-code and
refresh grants; the identity-assertion grant always raises `unsupported_grant_type`. So the tokens
of a registration whose every redirect URI is a vendor's own backend callback were obtained by that
vendor.

## Decision

In organization mode, a registration whose **every** redirect URI is one registry entry's own
callback is attested as that AI system: the call is recorded as `agent_source: "oauth_client"`, the
attested tier, and the card shows it as verified with the AI system's icon. No administrator step.

1. **Registry patterns.** `RegistryEntry` carries `redirect_uris`: exact strings, or one with a
   single `{id}` placeholder for one path segment (`[A-Za-z0-9_-]{1,128}`). They are matched with
   `fullmatch`, case-sensitively. Three entries have patterns, each the vendor's own callback:
   - `claude`: `https://claude.ai/api/mcp/auth_callback` (recorded in `tests/fixtures/ai_clients`);
   - `chatgpt`: `https://chatgpt.com/connector/oauth/{id}` (recorded; the id is the per-connector
     callback id);
   - `gemini-enterprise`: `https://vertexaisearch.cloud.google.com/oauth-redirect` (fixed, see
     [ADR 0110](0110-gemini-enterprise-is-registered-through-the-existing-register-endpoint.md)).
2. **All or nothing.** One redirect URI that matches no entry, or two URIs that match different
   entries, leaves the whole registration claimed: a single foreign URI is enough to send a code
   somewhere other than the vendor.
3. **Everything else stays claimed.** A loopback redirect (Claude Code, Gemini CLI, Claude Desktop
   through its extension) is a URI any local program can register, so those registrations keep the
   `client_info` tier and the card says the client *says* it is that system and is Not verified.
   Claude Desktop's custom connector most likely registers through claude.ai with the same redirect
   URI ([ADR 0094](0094-claude-clients-are-matched-by-their-observed-names.md)) and is then shown
   as Claude.

The attested lookup is keyed by the access token's `client_id`, which the authorization server
issued, and is tried before any name. A scoped token never reaches it.

### What this does not prove

Anything that can complete `/authorize` as a signed-in organization member can register its own
client under a vendor's redirect URI and read the authorization code before the vendor gets it. That
includes the member, and also a program using the member's browser session: the code is shown on
PrivacyFence's own consent continue page (`oauth_consent.render_continue_page`, a link plus a meta
refresh). The code is then redeemed with the secret and PKCE verifier the registrant holds, and that
member's own calls are shown as the vendor, verified. It never reaches another member's calls.

An administrator could not tell such a registration from the real one either, so the pin did not
close this gap. A future rule that keys on attested identity must not treat the attestation as a
defence against the member or against software running as them.

## Alternatives considered

- **Match on the host alone.** An open redirect anywhere on the vendor's domain would deliver the
  code elsewhere while the registration still looked like the vendor's.
- **An unrestricted prefix** (`https://chatgpt.com/connector/oauth/` followed by anything). Path
  segments, queries or fragments could then redirect off the vendor's fixed prefix. The `{id}`
  class excludes `/`, `.`, `?`, `#`, `%` and `@`.
- **A display-only tier** (verified-looking but not attested). Adds a third tier for no decision
  that uses it; the attested tier is already display-only today.
- **Dropping organization-mode attestation.** Leaves every web client Not verified and the card
  unable to say anything true about where a registration's tokens went.

## Consequences

claude.ai, ChatGPT and Gemini Enterprise are verified with no administrator step, on every member's
registration. Claude Code and other loopback clients stay Not verified, and there is nothing an
administrator can do about that. The self-spoofing gap above is accepted and documented. If the
real ChatGPT callback id contains a character outside the placeholder's class, ChatGPT stays
claimed: the failure is safe.

## Verification

- `tests/unit/test_agent_identity.py`: accepted and rejected redirect URIs, mixed and empty sets.
- `tests/unit/web/test_agent_attestation.py` (`TestRedirectAttestation`) and
  `tests/unit/web/test_ai_client_replay.py`: recorded registrations come out as `oauth_client` or
  `client_info`.

## Related

- [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md), [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md), [ADR 0094](0094-claude-clients-are-matched-by-their-observed-names.md), [ADR 0110](0110-gemini-enterprise-is-registered-through-the-existing-register-endpoint.md)
- [ADR 0225](0225-the-admin-ai-system-pin-is-removed.md) removes the pin this replaces.
