# Org mode attests an AI system by its redirect URI, and the admin pin goes

## Goal

In organization mode, an approval card shows claude.ai, ChatGPT and Gemini Enterprise as
**verified** with no admin action. PrivacyFence tells them apart by the redirect URI each one
registered with, because the authorization code can only ever reach a registered redirect URI. A
client whose redirect URI is a loopback address (Claude Code, Gemini CLI) or anything else keeps
the claimed tier: its card says it *says* it is that system and marks it **Not verified**, as
today. The admin's per-registration pin and the **Settings → AI systems** page that holds it are
removed. A pin had no effect beyond the card's badge and icon, it had to be repeated for every
user's registration, and the admin had no way to check what they were pinning. Nothing else in
an approval decision changes: no rule keys on attested identity today.

Decided in the planning conversation (no issue):

1. Redirect-URI attestation: OK. A redirect-matched client records `agent_source: "oauth_client"`,
   the attested tier.
2. Loopback-redirect clients stay claimed: OK.
3. The admin pin: removed entirely.
4. The gap this leaves, found during planning: anything that can complete `/authorize` as a
   signed-in org member can register its own client under a vendor's redirect URI and read the
   code before the vendor gets it. That includes the member, and also a program using the member's
   browser session. The code is in plain sight on PrivacyFence's own consent "continue" page
   (`oauth_consent.py:110-121`, `render_continue_page`, an `<a href>` plus a meta refresh). It is
   then redeemed with the secret and PKCE verifier the registrant holds, and that member's own
   calls are shown as that vendor, verified. It never reaches another member's calls. The user
   chose to keep the attested tier and record this gap in the ADR (option "Attested, gap
   documented"), rather than add a display-only tier or drop org-mode attestation.

Rejected in the conversation: pinning by name ("trust every registration called Claude"), which
reopens the DCR spoofing bypass ADR 0035 decision 3 was written against.

## Current state

- **Registry.** `src/privacyfence/agent_identity.py:98` defines `RegistryEntry` (`agent_id`,
  `display_name`, `client_names`, `name_templates`). `REGISTRY` (`:112`) has seven entries:
  `claude-code`, `claude`, `claude-desktop`, `chatgpt`, `gemini-cli`, `gemini-enterprise`,
  `cursor`. `entry_for_id` (`:164`) and `identify_registry_id` (`:172`) resolve an `agent_id` to an
  `AgentIdentity` with a given source. `AgentSource.OAUTH_CLIENT.is_attested()` is True (`:54`).
- **The pin store.** `src/privacyfence/web/agent_pins.py` (115 lines): `AgentPinStore` persists
  `client_id → AgentPin` to `org/agent_pins.json` (`pins_file_path()`).
- **Provider.** `src/privacyfence/web/oauth_provider.py`: imports the store (`:92`), builds
  `self.agent_pins` (`:275`), and has `pinned_agent_id()` (`:379`, only a live registration
  counts), `list_clients()` + `RegisteredClient` (`:388`, `:190`, used only by the pin page) and
  `has_client()` (`:400`, used only by the pin route). `client_name()` (`:368`) is the read-only
  per-call lookup the pin lookup is modelled on. Stored registrations keep their full
  `OAuthClientInformationFull`, `redirect_uris` included.
- **The redirect URI is enforced by the SDK.** `mcp/shared/auth.py:187`'s
  `OAuthClientInformationFull.validate_redirect_uri` refuses an `/authorize` whose `redirect_uri` is not
  in the registration's `redirect_uris`, and `mcp/server/auth/handlers/token.py:164-181` refuses a
  `/token` whose `redirect_uri` differs from the one the code was issued for. The provider issues
  tokens only through the authorization-code and refresh grants: `exchange_identity_assertion`
  (`oauth_provider.py:645`) always raises `unsupported_grant_type`. Scoped tokens never reach the
  pin lookup (`routes_mcp._scoped_agent`, `:112`).
- **Attribution.** `src/privacyfence/web/routes_mcp.py`: `PinnedAgentLookup` (`:184`),
  `_resolve_agent(..., pinned_agents=...)` (`:200-237`) tries the pin first (recorded as
  `OAUTH_CLIENT` through `identify_registry_id`), then a local override, then the DCR
  `client_name` and the handshake name (both `CLIENT_INFO`). `build_mcp_server` (`:340`),
  `build_mcp_asgi_app` (`:885`) and `mount_mcp` (`:932`) thread `pinned_agents` through. `server.py:1306`
  wires `pinned_agents=org.provider.pinned_agent_id` for org mode.
- **The settings surface.** `src/privacyfence/web/routes_settings.py`: `_ORG_ONLY_SENSITIVE_ACTIONS`
  (`:281`, only the two pin actions; also read by `_needs_step_up`, `:731`), the two actions in
  `_ORG_ALLOWED_ACTIONS` (`:1453`), `build_org_routes(oauth_provider=...)` (`:1465`, docstring
  `:1518-1523`, used only by the pin page), `"agents": _agents_state()` in the page state
  (`:1668`), `_agents_state` (`:1673-1702`), the action branches (`:2105-2111`) and
  `_pin_agent_client` (`:2133-2153`). `src/privacyfence/web/org_settings_scope.py:139-148`
  declares `pin_agent_client` and `unpin_agent_client`, the only ORG_MODE-only actions; its module
  docstring (`:13`) mentions them. `server.py:1406-1407` passes `oauth_provider=org.provider`.
- **The page.** `src/privacyfence/settings_window_html.py`: CSS (`:253` shares a rule with
  `.pf-audit-row`; `:271-276`), the `sections` default (`:332`), nav entry `['agents', 'AI systems']`
  (`:482`), `renderAgents` (`:1229-1279`), the `case 'agents'` dispatch (`:1829`), `_ALL_SECTIONS`
  (`:2421`) and `build_capabilities`'s `"agents"` key and docstring (`:2467-2490`).
- **Tests that cover pins:** `tests/unit/web/test_agent_attestation.py` (617 lines; classes
  `TestAgentPinStore`, `TestOrgPinCapture`, `TestPinActions`, `TestAgentsPageState`, plus
  `test_a_pin_outranks_an_override` in `TestLocalOverrideNeverAttests`),
  `tests/unit/web/test_ai_client_replay.py` (`pin=` replay and the `unpinned|pinned` README
  lines), `tests/unit/web/test_routes_mcp_agent_identity.py` (`pinned_agents=` at `:96-102`,
  `:283`, `:481`), `tests/unit/web/test_org_settings_scope.py` (`:22`, `:39-52`, `:104-107`,
  `:120-130`), `tests/unit/web/conftest.py:39-43`, `tests/unit/test_settings_window_html.py:907-909`,
  `tests/integration/test_browser_smoke.py:290` and `:2659-2672`.
- **Fixtures.** `tests/fixtures/ai_clients/{claude-ai,chatgpt,claude-code,gemini-cli}/` each have a
  `README.md` with `- Expected agent_id, unpinned:` and `- Expected agent_id, pinned:` lines. The
  recorded redirect URIs are `https://claude.ai/api/mcp/auth_callback` (claude.ai),
  `https://chatgpt.com/connector/oauth/dummy-callback-id` (ChatGPT; the real id was scrubbed),
  `http://localhost:50520/callback` (Claude Code) and `http://localhost:7777/oauth/callback`
  (Gemini CLI). Gemini Enterprise has no fixture. Its redirect URI is fixed at
  `https://vertexaisearch.cloud.google.com/oauth-redirect` (`docs/connect-gemini-enterprise.md:47`,
  ADR 0110).
- **Reference docs that describe pins:** `docs/org-mode-setup-guide.md` §11 (`AI systems`) and
  `:751`; `docs/how-it-works.md:232-240`; `docs/security-and-compliance.md:134`, `:239`, `:375-382`;
  `docs/approvals-and-policy.md:195`; `docs/ai-client-qa.md:116-118`;
  `docs/connect-claude-ai.md:109-113`, `:124`; `docs/connect-chatgpt.md:174-178`, `:192`;
  `docs/connect-gemini-enterprise.md:54`, `:175-182`, `:201`; `docs/connect-claude-code.md:165-166`;
  `docs/connect-claude-desktop.md:103-107`; `docs/coding-and-testing-guidelines.md:240`;
  `tests/fixtures/ai_clients/README.md:18-28`. The website (`website/ai-agents/*/index.html`,
  `website/enterprise/index.html:53`, `website/security/index.html:76`) also describes pins; see
  Risks.

## Design

### D1. Redirect patterns in the registry

`RegistryEntry` gains one field, after `name_templates`:

```python
    # Redirect URIs only this vendor's own backend receives a code at (ADR 0224). Exact strings,
    # or one with a single ``{id}`` placeholder standing for one path segment of the vendor's own
    # per-connector callback id. A registration all of whose redirect URIs match one entry's
    # patterns is attested as that entry.
    redirect_uris: tuple[str, ...] = ()
```

The new module constants, placed after `SERVER_PLACEHOLDER`:

```python
# The one placeholder a registry redirect pattern may carry: a vendor's per-connector callback id,
# one path segment of letters, digits, "_" and "-" (ADR 0224).
REDIRECT_ID_PLACEHOLDER = "{id}"
_REDIRECT_ID_PATTERN = r"[A-Za-z0-9_-]{1,128}"
```

The entries that get redirect patterns (no other entry changes):

| `agent_id` | `redirect_uris` |
|---|---|
| `claude` | `("https://claude.ai/api/mcp/auth_callback",)` |
| `chatgpt` | `("https://chatgpt.com/connector/oauth/{id}",)` |
| `gemini-enterprise` | `("https://vertexaisearch.cloud.google.com/oauth-redirect",)` |

Matching is a whole-string, **case-sensitive** regex built once at import:
`re.escape(prefix) + _REDIRECT_ID_PATTERN + re.escape(suffix)` for a pattern with `{id}`, and
`re.escape(pattern)` for one without, both used with `fullmatch`. The `{id}` segment's character
class contains no `/`, `.`, `?`, `#`, `%` or `@`, so no other path, query, fragment, port or user
info can follow the vendor's fixed prefix. Build the table like `_BY_TEMPLATE`:

```python
_BY_REDIRECT: tuple[tuple[re.Pattern[str], RegistryEntry], ...] = tuple(
    (_redirect_regex(pattern), entry) for entry in REGISTRY for pattern in entry.redirect_uris
)
```

with `_redirect_regex(pattern: str) -> re.Pattern[str]` (`partition(REDIRECT_ID_PLACEHOLDER)`;
no placeholder gives `re.compile(re.escape(pattern))`).

The new public function, placed after `entry_for_id`:

```python
def entry_for_redirect_uris(redirect_uris: Sequence[object]) -> RegistryEntry | None:
    """The registry entry every one of ``redirect_uris`` belongs to, or None (ADR 0224).

    None for an empty sequence, for any URI that matches no entry's redirect pattern, and for URIs
    that match two different entries: one unmatched or foreign redirect URI is enough to send a
    code somewhere other than the vendor, so the whole registration stays claimed. Each URI is
    compared as ``str(uri)``."""
```

Import `Sequence` from `collections.abc`. The module docstring's invariant list gains no new
invariant. Its sentence "Only `AgentSource.OVERRIDE` and `AgentSource.OAUTH_CLIENT` are attested"
stays.

### D2. The provider's per-call lookup

`OrgOAuthProvider` gains, after `client_name()`:

```python
    def attested_agent_id(self, client_id: str) -> str | None:
        """The registry ``agent_id`` this registration is attested as by its redirect URIs, or
        None -- read-only, and like ``client_name`` asked once per tool call (ADR 0224). The code
        for this ``client_id`` can only have gone to a registered redirect URI, so a registration
        whose every redirect URI is one vendor's own callback got its tokens through that vendor."""
        with self._lock:
            stored = self._clients.get(client_id)
            uris = tuple(stored.info.redirect_uris or ()) if stored is not None else ()
        entry = entry_for_redirect_uris(uris)
        return entry.agent_id if entry is not None else None
```

In phase p1 this method first returns an admin pin when there is one (see the p1 brief), so the
pin tests keep passing until p2 removes pins. After p2 it is exactly the code above.

### D3. Attribution

In `routes_mcp.py`, rename `PinnedAgentLookup` to `AttestedAgentLookup` and the keyword
`pinned_agents` to `attested_agents` everywhere (`_resolve_agent`, `build_mcp_server`,
`build_mcp_asgi_app`, `mount_mcp`, and `server.py:1306`, which passes
`attested_agents=org.provider.attested_agent_id`). Keep the logic of `_resolve_agent` as it is.
The attested lookup is still tried first and still recorded as `AgentSource.OAUTH_CLIENT` through
`identify_registry_id`, with the handshake version. The comment above `AttestedAgentLookup`
becomes:

```python
# Looks up the registry agent_id an access token's ``client_id`` is attested as by its registered
# redirect URIs, or None (ADR 0224). Org mode passes ``OrgOAuthProvider.attested_agent_id``; the
# redirect URI decides where the code went, which only a client the member authorizes can
# intercept (ADR 0224 says what that leaves open), so it is the one signal on this path that
# records ``oauth_client``.
```

`_resolve_agent`'s docstring item 1 becomes: "a registration whose redirect URIs are all one
registry entry's own callbacks (ADR 0224) -- `oauth_client`, the only attested source on this
path. The DCR `client_name` never overrides it." Its closing paragraph says that the attested
signal is keyed by the access token's `client_id`, which the authorization server issued, and
by redirect URIs the SDK enforces at `/authorize` and `/token`. Item 3's "since that is the one an
admin sees and pins" becomes "since that is the name the client registered with". In
`_scoped_agent`'s docstring (`:115`), "any org admin pin" becomes "any redirect-URI attestation".
`build_mcp_asgi_app`'s docstring (`:906-908`) says "``attested_agents`` (org mode only) is
``OrgOAuthProvider.attested_agent_id``, the redirect-URI attestation (ADR 0224)" in place of the
`pinned_agents` sentence. In `agent_identity.py`, `AgentSource.is_attested`'s docstring (`:55`)
becomes "True only for a signal the caller could not have chosen (ADR 0006 decision 3), within
the limit ADR 0224 records for a redirect-URI attestation." 

### D4. What goes

After p2 these no longer exist: `src/privacyfence/web/agent_pins.py`; in `oauth_provider.py`, the
`agent_pins` import and attribute, `pinned_agent_id`, `list_clients`, `RegisteredClient` and
`has_client`; in `routes_settings.py`, `_ORG_ONLY_SENSITIVE_ACTIONS` (and its use in
`_needs_step_up`, which becomes `action in _SENSITIVE_ACTIONS`), `pin_agent_client` and
`unpin_agent_client` in `_ORG_ALLOWED_ACTIONS`, the `oauth_provider` parameter of
`build_org_routes` and its docstring paragraph, the `"agents"` key of the org state,
`_agents_state`, both action branches and `_pin_agent_client`, plus any import only they used
(`REGISTRY`, `entry_for_id`, `sanitize_client_string`, `datetime`/`timezone`, `_relative_time`:
remove an import only when `ruff check .` reports it unused); the comment above `_ALLOWED_ACTIONS` (`:209-213`) ends its first
clause at "every action naming LOCAL_MODE, which is every action there is" and drops "except the
two org-only AI-system pin actions (ADR 0035 decision 3)"; the comment block above
`_ORG_ONLY_SENSITIVE_ACTIONS` (`:274-280`) goes with it; in the org state docstring (`:1604-1608`)
"AI systems (admin only, ADR 0035 decision 3) the OAuth provider's registrations and pins; " is
deleted; in `org_settings_scope.py`, the two
`ActionScope` entries and their comment block, with the module docstring's "every action there is
except the two org-only AI-system pin actions, which have no local-mode meaning at all" replaced
by "every action there is"; in `server.py`, the `oauth_provider=org.provider` argument and its
comment (`:1406-1407`); in `settings_window_html.py`, the `agents` section everywhere (CSS rules
`.pf-agents-*` and their comment at `:271`, with `:253` becoming `.pf-audit-row + .pf-audit-row { ... }`
alone, the comment at `:2478-2479` (keep only the Stats sentence), the `sections`
default, the nav entry, `renderAgents`, the dispatch case, `"agents"` in `_ALL_SECTIONS` and in
`build_capabilities`, and the docstring's "AI systems (ADR 0035 decision 3) and" so it reads
"Stats (ADR 0217) is org-admin-only and never shown in local mode").

An existing `org/agent_pins.json` on a deployment is left where it is and is no longer read. Only
the changelog and ADR 0225 say it can be deleted; reference docs describe the current version only
(`docs/README.md`), so they do not mention it. Nothing migrates pins: a pinned claude.ai,
ChatGPT or Gemini Enterprise registration becomes attested by its redirect URI anyway. A pinned
Claude Code registration goes back to the claimed tier, which is the point of the change.

The org-mode settings nav after p2 is, for an admin: General, Auto-accept, Privacy Filter, Audit
Log, Stats, About.

### D5. Test changes

- `tests/unit/web/test_org_settings_scope.py`: with no ORG_MODE-only action left,
  `test_every_action_names_local_mode_or_is_a_classified_org_only_action` becomes
  `test_every_action_has_a_local_mode_route`, asserting `LOCAL_MODE in scope.modes` for every
  action, with a comment saying the last ORG_MODE-only actions, the AI-system pins, were removed
  by ADR 0225 and a new one needs its own sensitive classification in `routes_settings`.
  `test_org_only_actions_are_exactly_the_org_only_sensitive_set` becomes
  `test_there_are_no_org_only_actions` (`assert _ORG_ONLY_ACTIONS == frozenset()`). Delete the
  parametrized `test_org_only_actions_are_never_permitted_in_local_mode`, since it would collect
  zero cases and pytest would report it skipped. Drop `_ORG_ONLY_SENSITIVE_ACTIONS` from the
  import, and delete the stale comment line "The org-only AI-system pin actions are admin-only but
  never local." at `:130`, ending the line before it (`:129`) with a period instead of "--".
- Fixture READMEs: replace the two `Expected agent_id, unpinned/pinned` lines with
  `- Expected agent_id: `<id>`` and `- Expected agent_source: `<source>``:

  | fixture | agent_id | agent_source |
  |---|---|---|
  | claude-ai | `claude` | `oauth_client` |
  | chatgpt | `chatgpt` | `oauth_client` |
  | claude-code | `claude-code` | `client_info` |
  | gemini-cli | `gemini-cli` | `client_info` |

  `tests/fixtures/ai_clients/README.md` describes the two lines: `agent_source` is `oauth_client`
  when the recorded redirect URIs are the vendor's own callback (ADR 0224), and `client_info`
  otherwise, with the claimed-name precedence (DCR `client_name` the registry matches, else a
  recognised `clientInfo.name`, else `unknown:<name>`, ADR 0094) unchanged. Its scrubbing rule
  "A redirect URI on the vendor's own domain stays as it is" gains: "and a per-connector id in it
  is replaced with `dummy-callback-id`, which keeps it within the registry's redirect pattern".
  Each per-client README's sentence that mentions pinning ("the pinned audit entry", "the pinned
  version here") becomes "the attested audit entry" (claude-ai, chatgpt) or "the audit entry"
  (claude-code). Gemini CLI's README has no such sentence.

## ADRs

- **0224**: In organization mode, a registration whose every redirect URI is one registry entry's
  own callback is attested as that AI system (`oauth_client`). The ADR records the gap: anything that
  can complete `/authorize` as a member, a program using their browser session included, can read
  the code (it is shown on the consent continue page) and present that member's own calls as the
  vendor, verified; it never reaches another member's calls; an admin could not tell such a
  registration from the real one either, so a pin did not close it; and the rejected alternatives: matching on
  host alone (an open redirect anywhere on the vendor's domain would deliver the code elsewhere),
  an unrestricted prefix, a display-only tier, and dropping org-mode attestation. Amends ADR 0006
  (decision 3's attested sources) and ADR 0035 (decision 3).
- **0225**: The admin's per-registration AI-system pin and the **Settings → AI systems** page are
  removed. It had no effect on any decision, had to be repeated per user and per re-registration,
  and asked the admin to check something they could not check. Rejected: pinning by name (reopens
  the DCR bypass), keeping the pin for loopback clients (an admin cannot verify those either).
  Supersedes ADR 0035 decision 3.

## Manual steps

None before implementation. After implementation, one live check on the test organization
deployment, in `manual_after` below, step by step on the
[manual steps page](https://claude.ai/artifact/F1v44PMqmGyoCmS2pmac2c).

## Risks and open questions

- **ChatGPT's real per-connector id.** The fixture's `dummy-callback-id` replaced the real one,
  whose character set was not recorded. If it contains a character outside `[A-Za-z0-9_-]`, real
  ChatGPT registrations stay claimed: the failure is safe, and the manual check finds it. The fix
  is a follow-up that widens `_REDIRECT_ID_PATTERN`. Do not widen it in this plan.
- **Claude Desktop's custom connector** most likely registers through claude.ai with the same
  redirect URI (ADR 0094), so it would be attested as `claude` ("Claude"), not as Claude Desktop.
  That is correct for the registry as it stands. The docs say so.
- **`str(AnyUrl)` normalization.** The matcher compares `str(uri)` of the stored pydantic URL. For
  the three patterns, that string equals the registered string. If a p1 unit test that registers
  `https://claude.ai/api/mcp/auth_callback` and expects attestation fails because `str()` differs,
  for example by a trailing slash, the worker stops with `status=blocked` and does not loosen the
  matcher.
- **Other tests register the default client.** `tests/unit/web/conftest.py:25`'s
  `CLAUDE_REDIRECT_URI` is claude.ai's real callback. A test that wires
  `attested_agents=provider.attested_agent_id` and registers the default client is now attested.
  Only `test_ai_client_replay.py` wires it today. If any test outside a phase's `touches` fails on
  an `agent_source` expectation, the worker stops with `status=blocked`.
- **The website** describes pins on `website/ai-agents/index.html:80`, five `website/ai-agents/*/index.html` pages,
  `website/enterprise/index.html` and `website/security/index.html`. The site deploys from `main`
  but documents the released version
  ([ADR 0052](adr/0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md),
  [ADR 0099](adr/0099-an-ai-agents-website-page-lands-after-the-stable-release-whose-docs-carries-its-setup-doc.md)).
  So the website change is a follow-up PR after the stable release that carries this, not part
  of this plan.

## Implementation manifest

```yaml
plan_slug: org-agent-redirect-attestation
feature_branch: feature/org-agent-redirect-attestation
max_parallel: 1
manual_steps_artifact: https://claude.ai/artifact/F1v44PMqmGyoCmS2pmac2c
manual_steps_source: docs/org-agent-redirect-attestation-plan-manual-steps.html
manual_after:
  - id: ma1-live-attribution
    title: On the test organization deployment, claude.ai is verified without a pin and Claude Code is not
    why: CI replays recorded registrations only; whether today's real claude.ai and ChatGPT callbacks match the registry patterns needs the real clients
verify_after_merge:
  - python3 -m pytest tests/unit/web/test_agent_attestation.py tests/unit/web/test_ai_client_replay.py tests/unit/web/test_routes_mcp_agent_identity.py tests/unit/web/test_org_settings_scope.py tests/unit/test_settings_window_html.py tests/unit/test_agent_identity.py -q
final_checks:
  - docs/org-agent-redirect-attestation-plan.md and docs/org-agent-redirect-attestation-plan-manual-steps.html are deleted and nothing links to them
  - docs/adr/0224-*.md and docs/adr/0225-*.md exist, are Accepted, and are in docs/adr/README.md's index
  - CHANGELOG.md has the entries under "## [Unreleased]" and no new version heading
  - git grep -n "pin_agent_client\|AgentPinStore\|pinned_agent\|renderAgents" -- src tests prints nothing
phases:
  - id: p1-redirect-attestation
    title: Attest a registration by its redirect URIs (pins still work)
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/agent_identity.py
      - src/privacyfence/web/oauth_provider.py
      - src/privacyfence/web/routes_mcp.py
      - src/privacyfence/web/server.py
      - tests/unit/test_agent_identity.py
      - tests/unit/web/test_agent_attestation.py
      - tests/unit/web/test_ai_client_replay.py
      - tests/unit/web/test_routes_mcp_agent_identity.py
      - tests/fixtures/ai_clients/README.md
      - tests/fixtures/ai_clients/*/README.md
    brief: |
      Read docs/org-agent-redirect-attestation-plan.md (Design D1-D3, D5's fixture table) and
      docs/adr/0006-*.md, docs/adr/0035-*.md, docs/adr/0094-*.md first. Must-read docs:
      CLAUDE.md, CONTRIBUTING.md, docs/releasing.md, docs/coding-and-testing-guidelines.md,
      docs/testing-policy.md, docs/adr/README.md.

      1. src/privacyfence/agent_identity.py: add REDIRECT_ID_PLACEHOLDER, _REDIRECT_ID_PATTERN,
         the RegistryEntry.redirect_uris field, the redirect_uris values for "claude", "chatgpt"
         and "gemini-enterprise" (pass them as the keyword redirect_uris=...), _redirect_regex,
         _BY_REDIRECT and entry_for_redirect_uris exactly as in Design D1. Update the comment
         above REGISTRY to add "and ADR 0224 (redirect URIs)" after "extended by ADR 0109".
      2. Tests for step 1: extend tests/unit/test_agent_identity.py, in the file's existing style:
         - "https://claude.ai/api/mcp/auth_callback" gives the "claude" entry;
         - "https://chatgpt.com/connector/oauth/dummy-callback-id" gives "chatgpt";
         - "https://vertexaisearch.cloud.google.com/oauth-redirect" gives "gemini-enterprise";
         - each of these gives None: "http://claude.ai/api/mcp/auth_callback",
           "https://claude.ai/api/mcp/auth_callback/x", "https://claude.ai/api/mcp/auth_callback?x=1",
           "https://evil.example/api/mcp/auth_callback", "https://claude.ai.evil.example/api/mcp/auth_callback",
           "https://chatgpt.com/connector/oauth/a/b", "https://chatgpt.com/connector/oauth/",
           "https://chatgpt.com/connector/oauth/a.b", "https://chatgpt.com/connector/oauth/" + "a" * 129;
         - an empty sequence gives None;
         - [claude callback, "http://localhost:1/cb"] gives None;
         - [claude callback, chatgpt callback] gives None;
         - [claude callback, claude callback] gives "claude";
         - a pydantic AnyUrl("https://claude.ai/api/mcp/auth_callback") element gives "claude".
      3. src/privacyfence/web/oauth_provider.py: import entry_for_redirect_uris from
         ..agent_identity and add attested_agent_id as in Design D2, except that in this phase
         its body first returns the admin pin when there is one:
             pinned = self.pinned_agent_id(client_id)
             if pinned is not None:
                 return pinned
         followed by the D2 body. Leave pinned_agent_id and everything else about pins as it is.
      4. src/privacyfence/web/routes_mcp.py, src/privacyfence/web/server.py and
         AgentSource.is_attested's docstring in agent_identity.py: the renames and
         comment/docstring changes in Design D3. server.py:1306 passes
         attested_agents=org.provider.attested_agent_id. In D3's _resolve_agent item 1, add for
         this phase only "or an admin's pin of it (ADR 0035 decision 3)" after "own callbacks
         (ADR 0224)". p2 removes that clause again.
      5. Rename the keyword in tests: tests/unit/web/test_routes_mcp_agent_identity.py (the
         helper at :96-102 and the calls at :283 and :481; rename only, same lambdas; also rename
         test_a_pin_still_wins_over_a_recognised_handshake_name (:278) to
         test_an_attestation_still_wins_over_a_recognised_handshake_name, and the comment at
         :478 "and an org admin's pin too" to "and a redirect-URI attestation too") and
         tests/unit/web/test_agent_attestation.py (_resolve_org at :90-93 passes
         attested_agents=provider.attested_agent_id; :253 and every other pinned_agents= keyword
         becomes attested_agents=).
      6. tests/unit/web/test_agent_attestation.py: add a class TestRedirectAttestation after
         TestOrgPinCapture, with a helper that registers a client with given redirect_uris
         (copy _register, adding a redirect_uris parameter). Tests:
         - a registration with only "https://claude.ai/api/mcp/auth_callback" and client_name
           "Totally Not Claude" resolves (via _resolve_org, handshake "evil") to id "claude",
           source OAUTH_CLIENT;
         - the same for chatgpt (".../connector/oauth/abc123") -> "chatgpt" and Gemini
           Enterprise -> "gemini-enterprise";
         - a registration with "http://localhost:50520/callback" and client_name
           "Claude Code (privacyfence)" resolves to "claude-code" with source CLIENT_INFO;
         - a registration with the claude callback plus "http://localhost:1/cb" resolves with
           source CLIENT_INFO;
         - attested_agent_id of an unknown client_id is None;
         - attested_agent_id does not change last_used_at (copy the approach of
           test_listing_clients_does_not_count_as_using_them).
      7. tests/unit/web/test_ai_client_replay.py and tests/fixtures/ai_clients/: Design D5's
         fixture lines and README text. In the test: EXPECTED_RE matches
         r"^- Expected (agent_id|agent_source): `([^`]+)`$"; _load requires exactly those two
         keys; RecordedClient has expected_agent_id and expected_agent_source; _build_app passes
         attested_agents=provider.attested_agent_id; _replay loses its pin parameter and the
         pinning block; the two parametrized tests become one,
         test_handshake_succeeds_and_is_attributed(client_dir, ...), asserting
         {"agent_id": recorded.expected_agent_id, "agent_source": recorded.expected_agent_source}.
         Rewrite the module docstring's second paragraph: the call is attributed to the AI
         system its redirect URIs attest (ADR 0224), oauth_client, or else to the client's own
         claims, client_info. ADMIN becomes unused; delete it.
      8. Run: ruff check . ; python3 -m pytest tests/unit -q ; python3 -m mypy src/privacyfence ;
         python3 scripts/mypy_strict_modules.py. All must pass.

      Stop conditions (stop with status=blocked, do not improvise):
      - the test for AnyUrl("https://claude.ai/api/mcp/auth_callback") fails because str() of
        the URL is not the registered string;
      - the claude-ai or chatgpt replay does not come out as oauth_client;
      - a test outside this phase's touches fails on an agent_source or agent_id expectation.
      No CHANGELOG or docs/ change in this phase.
    acceptance:
      - python3 -m pytest tests/unit/test_agent_identity.py -q passes
      - python3 -m pytest tests/unit/web/test_agent_attestation.py -q -k TestRedirectAttestation passes
      - python3 -m pytest tests/unit/web/test_ai_client_replay.py -q passes
      - python3 -m pytest tests/unit/web/test_ai_client_replay.py --collect-only -q -k test_handshake_succeeds_and_is_attributed | grep -c "::test_handshake_succeeds_and_is_attributed\[" prints 4
      - git grep -n "pinned_agents\|PinnedAgentLookup" -- src tests prints nothing
      - grep -n "Expected agent_source: \`oauth_client\`" tests/fixtures/ai_clients/claude-ai/README.md tests/fixtures/ai_clients/chatgpt/README.md matches both
      - ruff check . passes and python3 -m mypy src/privacyfence passes

  - id: p2-remove-pins
    title: Remove the admin AI-system pin and the AI systems settings page
    depends_on: [p1-redirect-attestation]
    complexity: M
    touches:
      - src/privacyfence/web/agent_pins.py
      - src/privacyfence/web/oauth_provider.py
      - src/privacyfence/web/routes_mcp.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/org_settings_scope.py
      - src/privacyfence/web/server.py
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/agent_identity.py
      - tests/unit/web/test_agent_attestation.py
      - tests/unit/web/test_org_settings_scope.py
      - tests/unit/web/conftest.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_browser_smoke.py
      - tests/integration/ui_review_capture.py
    brief: |
      Read docs/org-agent-redirect-attestation-plan.md (Design D2, D3, D4, D5) first.
      Must-read docs: CLAUDE.md, CONTRIBUTING.md, docs/releasing.md,
      docs/coding-and-testing-guidelines.md, docs/testing-policy.md, docs/adr/README.md.

      1. git rm src/privacyfence/web/agent_pins.py.
      2. src/privacyfence/web/oauth_provider.py: everything Design D4 lists for this file.
         attested_agent_id becomes exactly the D2 body (no pin lookup). Fix the comments that
         mention the pin page: the RegisteredClient docstring goes with the class; read
         "_StoredClient"'s docstring and leave it unless it names pins.
      3. src/privacyfence/web/routes_mcp.py: remove p1's "or an admin's pin of it (ADR 0035
         decision 3)" clause from _resolve_agent's docstring. grep -n -i "pin" in the file and
         reword any other sentence that still says an admin pins a client so it names the
         redirect-URI attestation (ADR 0224) instead. Do not touch the unrelated "the pin is a
         range" (mcp version) sentences or "pinned to a dead session". Then run
         grep -n -i "pin" src/privacyfence/web/routes_settings.py src/privacyfence/settings_window_html.py
         src/privacyfence/web/oauth_provider.py src/privacyfence/web/server.py and fix every remaining
         sentence about AI-system pins with Design D4's wording; leave signing-key pins, PINs,
         peek pinned_base_url and grist_server_url_pinned alone.
      4. src/privacyfence/web/routes_settings.py, org_settings_scope.py, server.py and
         settings_window_html.py: everything Design D4 lists for them. In
         routes_settings.py's org state, delete the "agents": ... line outright.
      5. src/privacyfence/agent_identity.py: entry_for_id's docstring "what an org pin or a local
         override names (ADR 0035 decision 3, ADR 0006 option D)" becomes "what a redirect-URI
         attestation (ADR 0224) or a local override names (ADR 0006 option D)", and
         identify_registry_id's "an admin pin or a local override named" becomes "a redirect-URI
         attestation or a local override named".
      6. tests/unit/web/test_agent_attestation.py: delete TestAgentPinStore, TestOrgPinCapture
         except the two tests that do not involve pins (move
         test_an_unpinned_registration_stays_claimed as test_a_registration_with_a_foreign_redirect_stays_claimed
         and test_an_unpinned_gemini_enterprise_registration_is_attributed_by_its_dcr_name as
         test_a_gemini_enterprise_name_with_a_foreign_redirect_is_claimed into
         TestRedirectAttestation, unchanged otherwise; also move test_a_pin_needs_an_access_token
         (:252-254) there as test_an_attestation_needs_an_access_token, with its lambda kept), TestPinActions, TestAgentsPageState, and
         test_a_pin_outranks_an_override (replace the last with
         test_a_redirect_attestation_outranks_an_override: a registration with the claude
         callback, an override whose selector matches the handshake name, resolves to "claude"
         with source OAUTH_CLIENT). Remove the agent_pins import and the pins_file_path
         monkeypatch from _provider. Rewrite the module docstring's org-mode bullet: "Org mode:
         a registration whose redirect URIs are all one AI system's own callbacks is attested
         as that system (ADR 0224), oauth_client; any other stays client_info, whatever its
         client_name." Remove imports ruff reports unused.
      7. tests/unit/web/test_org_settings_scope.py: Design D5's first bullet.
      8. tests/unit/web/conftest.py: delete the pins_file_path monkeypatch line (:43) and
         "and the AI-systems pin store" from the docstring (:39).
      9. tests/unit/test_settings_window_html.py: in test_local_mode_default_hides_nothing
         (:855-867) delete the `caps["sections"]["agents"]` assertion (:862), drop "agents" from
         the tuple at :866, and change the comment at :859-861 to "Stats is org-only, and Connect
         an AI client needs an /mcp URL the caller did not pass; every other section stays."
         Delete test_ai_systems_page_is_org_admin_only
         and add test_there_is_no_ai_systems_page asserting "agents" is not in
         capabilities["sections"] for org admin, org non-admin and local mode (use the same
         self._extract_capabilities(build_html(...)) calls as the deleted test, and
         build_html(_make_state(), mode="local") for local if that is how the file builds local
         mode; read the file's other tests to see which).
      10. tests/integration/test_browser_smoke.py: delete the pins_file_path monkeypatch (:290);
          in test_org_settings_page_renders_for_admin, the expected nav becomes
          ["General", "Auto-accept", "Privacy Filter", "Audit Log", "Stats", "About"] and the
          agents-page block (:2667-2672, through the org-settings-admin-agents screenshot) is
          deleted; remove "agents" from _ADMIN_SECTIONS (:2847). In
          tests/integration/ui_review_capture.py remove "agents" from the parametrize list (:203).
      11. Run: ruff check . ; python3 -m pytest tests/unit -q ; python3 -m mypy src/privacyfence ;
          python3 scripts/mypy_strict_modules.py ; bandit -c pyproject.toml -r src ;
          python3 -m pytest tests/integration/test_browser_smoke.py -q -k "org_settings or test_settings" (read the
          session-start hook's Chromium line; if browser tests report SKIPPED, say so in the
          phase report and do not count it as a pass). All must pass.

      Stop conditions (stop with status=blocked):
      - something outside this phase's touches imports agent_pins, list_clients,
        RegisteredClient, has_client or _ORG_ONLY_SENSITIVE_ACTIONS (grep -rn src tests scripts);
      - removing "agents" from build_capabilities breaks a test that is not listed here.
      No CHANGELOG or docs/ change in this phase.
    acceptance:
      - test ! -e src/privacyfence/web/agent_pins.py
      - git grep -n "agent_pins\|pin_agent_client\|unpin_agent_client\|pinned_agent\|list_clients\|RegisteredClient\|has_client\|_ORG_ONLY_SENSITIVE_ACTIONS\|renderAgents\|pf-agents" -- src tests scripts prints nothing
      - python3 -m pytest tests/unit/web/test_agent_attestation.py tests/unit/web/test_org_settings_scope.py tests/unit/test_settings_window_html.py tests/unit/web/test_ai_client_replay.py -q passes
      - python3 -m pytest tests/unit/web/test_org_settings_scope.py -q reports no skipped tests
      - python3 -m pytest tests/unit -q passes
      - ruff check . , python3 -m mypy src/privacyfence and bandit -c pyproject.toml -r src pass

  - id: p3-docs-adrs-retire
    title: ADRs 0224 and 0225, reference docs, changelog, and retire the plan
    depends_on: [p2-remove-pins]
    complexity: M
    touches:
      - docs/adr/0224-*.md
      - docs/adr/0225-*.md
      - docs/adr/README.md
      - docs/adr/0006-*.md
      - docs/adr/0035-*.md
      - docs/org-mode-setup-guide.md
      - docs/how-it-works.md
      - docs/security-and-compliance.md
      - docs/approvals-and-policy.md
      - docs/ai-client-qa.md
      - docs/connect-claude-ai.md
      - docs/connect-chatgpt.md
      - docs/connect-gemini-enterprise.md
      - docs/connect-claude-code.md
      - docs/connect-claude-desktop.md
      - docs/coding-and-testing-guidelines.md
      - CHANGELOG.md
      - docs/org-agent-redirect-attestation-plan.md
      - docs/org-agent-redirect-attestation-plan-manual-steps.html
    brief: |
      Read docs/org-agent-redirect-attestation-plan.md in full (Goal, Design, ADRs, Risks) and
      docs/adr/README.md (template, rules, index format) first. Must-read docs: CLAUDE.md,
      CONTRIBUTING.md, docs/releasing.md, docs/coding-and-testing-guidelines.md,
      docs/testing-policy.md, and docs/README.md (reference docs describe the current version only). ADRs never
      link to the plan document.

      1. docs/adr/0224-an-org-registration-is-attested-by-its-redirect-uris.md, Accepted, dated
         the day you write it, "Decided by the maintainer in planning". Title: "ADR 0224: In
         organization mode a registration is attested by its redirect URIs". Context: ADR 0035
         decision 3 made the admin pin the only attested org signal; it is cosmetic today (no
         decision keys on it; only the card's icon and badge, approval_window_html.py and
         approval_list_html.py), per-registration, and the admin cannot check it. Decision: D1-D3
         of the plan, including the three patterns and why each is the vendor's own callback
         (claude.ai and ChatGPT from tests/fixtures/ai_clients, Gemini Enterprise from ADR 0110);
         the SDK's redirect enforcement (mcp/shared/auth.py validate_redirect_uri and the /token
         redirect check); only authorization-code and refresh grants exist. A section "What this
         does not prove" with the gap from the plan's Goal item 4, stated plainly: anything that
         can complete /authorize as the member, including a program using their browser
         session, can read the code (shown on the consent continue page,
         oauth_consent.render_continue_page) and present that member's own calls as the vendor,
         verified; it never reaches another member's calls; an admin could not tell such a
         registration from the real one either, so a pin did not close it; and a future rule
         keying on attested identity must not treat it as a defence against the member or
         software running as them. Alternatives: host-only matching, an unrestricted
         prefix, a display-only tier, dropping org attestation (the reasons are in the plan's
         ADRs section). Status line: "Amends ADR 0006 and ADR 0035 (decision 3)."
      2. docs/adr/0225-the-admin-ai-system-pin-is-removed.md, Accepted. Decision and
         alternatives from the plan's ADRs section and Design D4 (including that
         org/agent_pins.json is left on disk unread). Status: "Supersedes ADR 0035 decision 3."
      3. Add "Amended by [ADR 0224](...)" to ADR 0006's Status, and "Amended by ADR 0224" plus
         "Decision 3 superseded by [ADR 0225](...)" to ADR 0035's Status, each as one line in
         the style of the existing "Amended by" lines. Change nothing else in either body.
      4. docs/adr/README.md index: append rows for 0224 ("Accepted; amends 0006, 0035") and 0225
         ("Accepted; supersedes 0035 decision 3"). The 0006 row's status becomes "Accepted;
         implemented; amended by 0035, 0037, 0224"; the 0035 row's becomes "Accepted; amended by
         0224; decision 3 superseded by 0225".
      5. docs/org-mode-setup-guide.md §11: keep the heading "## 11. AI systems" (other docs link
         #11-ai-systems). Rewrite the table's first row to "The client registered only with its
         vendor's own redirect URI (claude.ai, ChatGPT, Gemini Enterprise) | oauth_client |
         Verified, with the AI system's icon", and replace the pin paragraphs and bullets with:
         why a redirect URI proves where the code went; the three vendors and their callbacks;
         that Claude Code, Claude Desktop through the extension, and anything with a loopback
         redirect stay Not verified because any local program can register one; that Claude
         Desktop's custom connector most likely registers through claude.ai and is then shown
         as Claude; the self-spoofing gap in one sentence; and, as its last
         sentence, exactly "There is nothing for an administrator to do: which clients are
         verified follows from how they registered." Do not mention pins, agent_pins.json or a
         settings page in §11. Links to ADR 0224, ADR 0225 and the existing ADR 0036/0037
         links. Elsewhere in the same file:
         - :50: "the install-wide privacy policy, the **AI systems** page and the **Stats** page"
           -> "the install-wide privacy policy and the **Stats** page";
         - :150: "**General**, **Privacy Filter**, **AI systems** and **Stats** pages" ->
           "**General**, **Privacy Filter** and **Stats** pages";
         - :750-751: "Each client registration shows up on the admin's **AI systems** page
           ([section 11](#11-ai-systems))." -> "[Section 11](#11-ai-systems) says which clients
           are shown as verified.";
         - :811: delete the `| AI systems | | yes |` row;
         - :910: delete ", and pinning or unpinning an AI system" so the sentence lists the
           remaining sensitive writes (read the whole sentence and keep its grammar);
         - :1054: delete the `org/agent_pins.json` row;
         - :1295: "**Admins do not see Privacy Filter or AI systems.**" -> "**Admins do not see
           Privacy Filter or Stats.**" (check the following sentences still fit; they are about
           the admin claim).
      6. Each remaining doc in touches: replace every pin sentence with the redirect-URI
         behaviour for that client (verified with no admin step for claude.ai, ChatGPT, Gemini
         Enterprise; Not verified for Claude Code and Claude Desktop, with nothing an admin can
         do about it), and link #11-ai-systems for details. Specifically:
         docs/how-it-works.md:239-240 table rows; docs/security-and-compliance.md:134 (delete the
         bullet), :239 ("outranks an organization pin" -> "outranks a redirect-URI
         attestation"), :375-382 (the oauth_client row and the "only verified identity"
         paragraph); docs/approvals-and-policy.md:195; docs/ai-client-qa.md:22 (the bullet becomes "agent attribution (`agent_identity.py`, its
         name and redirect patterns);") step 6 at :114-118 (becomes
         "Attribution tier (organization mode only)": check that a web client's cards are
         verified with agent_source=oauth_client without any admin step, and that a loopback
         client's are Not verified), and :143 ("that after pinning it on **Settings → AI
         systems**, the next card said verified" -> "that its cards are verified without any
         admin step"). Leave the dated results table in docs/ai-client-qa.md (the rows starting
         with "| " after the "Results" heading, around :160) unchanged: it is a historical record. the "Every card says Not verified" troubleshooting rows in
         connect-claude-ai.md, connect-chatgpt.md and connect-gemini-enterprise.md (now: "Expected
         when the registration's redirect URI is not the vendor's own callback; see
         [AI systems](org-mode-setup-guide.md#11-ai-systems)."); connect-gemini-enterprise.md:54-55 ("and the
         registration an administrator pins (see ...)" -> "(see [How it is identified](#how-it-is-identified))"),
         :175-182 (the pin sentences and the release note about pinning go; say the cards are
         verified because the registration carries Google's own redirect URI); docs/coding-and-testing-guidelines.md:240 (the source-module cell becomes
         `agent_overrides.py` alone). docs/how-it-works.md:234 and
         docs/security-and-compliance.md:382 cite ADR 0224 for the oauth_client tier alongside
         their ADR 0035 link.
         Then grep -rn -i "pin" over these files and fix any remaining sentence about AI-system
         pins. Leave signing-key, release, Peek and Grist pins alone.
      7. CHANGELOG.md, directly under "## [Unreleased]" (empty today; if another branch has since
         added "### Added" there, put these after it): "### Changed" with "**claude.ai, ChatGPT and Gemini Enterprise are verified
         without an admin step.** In organization mode, a client that registered only with its
         vendor's own redirect URI is shown as verified. Claude Code and other clients with a
         local redirect stay Not verified." Then "### Removed" with: "The **AI systems** settings page and its per-registration pins. Pins are no
         longer read; an organization deployment can delete `org/agent_pins.json`."
      8. git rm docs/org-agent-redirect-attestation-plan.md
         docs/org-agent-redirect-attestation-plan-manual-steps.html; grep -rn
         "org-agent-redirect-attestation-plan" . (excluding .git) finds nothing.
      9. Run: ruff check . ; python3 -m pytest tests/unit -q (docs tests such as
         tests/unit/test_docs_configuration_reference.py run here). All must pass.

      Stop condition: a docs test fails on a link or anchor you did not change, in which case
      stop with status=blocked.
    acceptance:
      - ls docs/adr/0224-*.md docs/adr/0225-*.md lists both, and grep -n "^Accepted" matches in each
      - grep -n "0224\|0225" docs/adr/README.md matches both rows
      - grep -rn -i "pin it\|admin pin\|pinned the\|pins your registration\|Stale pins\|Settings → AI systems\|AI systems\*\* page\|agent_pins" docs --include=*.md | grep -v "^docs/adr/" | grep -v "^docs/ai-client-qa.md:[0-9]*:|" prints nothing
      - test ! -e docs/org-agent-redirect-attestation-plan.md && test ! -e docs/org-agent-redirect-attestation-plan-manual-steps.html
      - awk '/^## \[Unreleased\]/{f=1} f&&/^## \[[0-9]/{exit} f' CHANGELOG.md | grep -c "agent_pins.json" prints 1
      - python3 -m pytest tests/unit -q passes
```
