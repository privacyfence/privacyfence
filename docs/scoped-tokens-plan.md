# Scoped AI client tokens: named, rule- and tool-scoped, expiring credentials for `/mcp`

## Goal

Today every AI client of one person uses the same credential: the local MCP token (one per OS
account, no expiry, ADR 0008, ADR 0104) or, in organization mode, an OAuth sign-in. Whatever holds
it can use every rule the person has and see every tool. A scheduled job such as a
`privacyfence-sync` recipe needs much less.

After this change a person can create **scoped tokens** in Settings, in both modes:

- A scoped token has a label (`sales-sync`), an expiry (90 days by default) and a scope: the
  auto-accept rules it may use and the tools it may call.
- A client presenting it sees only the tools in its scope, and a call auto-accepts only through one
  of its rules. Anything else is denied at once. A scoped token never gets an approval card and
  never leaves a pending approval, like an unattended session (ADR 0015).
- Each token is revoked on its own, without affecting the person's other clients. The audit log
  names the token that made each call.
- Expiry is visible ahead of time: Settings flags tokens with 14 days or fewer left, and
  `privacyfence_status` reports the token's expiry to the client.
- In organization mode the administrator turns the feature on in the organization config bundle
  and caps the lifetime. A person's scoped tokens go with them when their access is revoked for
  good: an administrator can revoke all of a person's tokens from the server's command line, and
  the provider's revoke-everything path takes them too.

`privacyfence-sync` reads its token from `PRIVACYFENCE_TOKEN` (or `--token-env`) and never
interprets it, so in local mode it works with a scoped token unchanged. In organization mode a
scoped token is used the same way, as a bearer token, instead of `privacyfence-sync login`.

There is no issue; the request came from the user in a session.

## Current state

### Local-mode token

- `web/mcp_auth.py`: `_mcp_token_path(principal)` (`:47-59`) is `handoff_dir()/mcp_token` on an
  unseparated install, `authority_dir(principal)/mcp_token` on a separated one.
  `_write_new_token` (`:99-110`) uses `secrets.token_hex(32)`. `PerUserTokenVerifier`
  (`:113-155`) keeps `sha256(token) -> principal_id` in memory and returns
  `AccessToken(token, client_id="local", scopes=[], subject=principal_id)`; it never sets
  `expires_at`. `principal_from_access_token` (`:201-237`) maps `client_id == "local"` to the
  local principal (or `Principal(id=subject)`), anything else to `Principal(id=subject or
  client_id, email/display_name/is_admin from claims)`.
- The SDK's `BearerAuthBackend` already refuses an `AccessToken` whose `expires_at` has passed.
- Wiring (`web/server.py`): `self.mcp_verifier = PerUserTokenVerifier()` in local mode
  (`:1290-1292`), preloaded and registered at `:1325-1333`, passed to `mount_mcp(...,
  verifier=...)` (`:959-960`) and to `mount_file_bridge(...)` (`:974`). Org mode passes
  `verifier=org.provider` to `mount_mcp` (`:1105-1108`).
- One auth stack serves `/mcp` in both modes (`web/routes_mcp.py` `build_mcp_asgi_app`,
  `:768-812`): `AuthenticationMiddleware(BearerAuthBackend(verifier))` → `AuthContextMiddleware`
  → `RequireAuthMiddleware(required_scopes=[])`. The only seam is the `verifier` argument.
- The SDK binds a stateful session to `(client_id, claims["iss"], subject)`
  (`streamable_http_manager.py:266-279`), so a token with its own `client_id` gets its own
  sessions.

### Org-mode auth

- `web/oauth_provider.py` `OrgOAuthProvider`: access tokens in memory (`_access_tokens`, `:258`),
  `load_access_token` (`:589-597`), `verify_token` (`:599-604`), `_mint_tokens` (`:660-701`,
  `client_id=<DCR client>`, `subject=principal.id`, `claims={email, display_name, is_admin}`).
  `revoke_all_for_principal(principal_id)` (`:606`) exists and **has no caller**: org mode has no
  "sign out everywhere" action today (`org_session.py:109` calls it future work).
- ADR 0072: org mode persists only sealed refresh tokens; nothing else secret is at rest.

### After auth: one call

- `handle_list_tools` (`routes_mcp.py:376-401`) lists every connector tool of the principal plus
  `mcp_tools.META_TOOLS`. `handle_call_tool` (`:403-477`) resolves `principal` and `agent`, enters
  `principal_scope`, `agent_scope` and `local_files.call_context`, and dispatches to
  `_dispatch_meta_tool` (`:502-534`) or `_dispatch_connector_tool` (`:485-491`). Any exception
  becomes `mcp_tools.error_result(public_message(exc))`; `GateDeniedError` text is shown.
- `_resolve_agent` (`:158-197`) ranks an org admin pin (`AgentSource.OAUTH_CLIENT`) over a local
  override relabel and the client's own name (`CLIENT_INFO`). `AgentIdentity(id, name, version,
  source)` is in `agent_identity.py:60-70`. ADR 0037 keeps `AgentSource.OVERRIDE` "unused, for the
  per-credential override".
- `McpDispatcher.call` (`mcp_dispatch.py:241-245`) enters `unattended_scope(session_key in
  self._unattended_sessions)`. `begin_unattended_session` (`:595-610`) raises unless
  `unattended_sessions.enabled`.
- `gate.py`: `_evaluate_auto_accept` (`:682-702`) matches the cached rules
  (`get_policy_v2_store_rules()`), then the temp-accept window. `preflight_auto_accept`
  (`:705-720`) serves `privacyfence_check_policy`. The review gate (`:1086`) and the popup gate
  (`:1219`) call `_deny_unattended` (`:1479-1496`) when `is_unattended()`, after both auto-accept
  checks and before any pending registration. `propose_policy_change` refuses unattended
  (`:1420-1429`).

### Settings

- Local "AI clients" section: `renderAiClient()` (`settings_window_html.py:1121-1157`), data in
  `window.__pfAiClient` (`build_html(..., ai_client=...)`, `:1850-1854`), org mode hard-codes it
  off (`:1788`). Routes `POST /api/settings/mcp_token/copy` and `/rotate`
  (`web/routes_settings.py:996-1070`); rotate checks `confirmed`, a human session where required,
  and the passkey step-up through `_verify_or_challenge` with `_action_fingerprint`. Bespoke routes
  must be listed in `_BESPOKE_SENSITIVE_ROUTE_PATHS` or `_BESPOKE_EXEMPT_ROUTE_PATHS`
  (`:303-325`), or `build_routes` raises.
- The token reaches the clipboard through the companion: `control_channel.send_mcp_token(token)`
  (`:2394-2403`) sends `COPY MCP <token>`, accepted when it matches `_MCP_TOKEN_PATTERN =
  re.compile(r"\A[A-Za-z0-9_-]{32,128}\Z")` (`:899`). No web response carries a token (ADR 0104).
- Org Settings: `build_org_routes` (`routes_settings.py:1154`), `_ORG_ALLOWED_ACTIONS`
  (`:1145-1151`), `settings_action` (`:1610-1714`: session, CSRF, origin, `is_action_permitted`,
  `_guard_step_up`). The only org page that shows a secret once is the passkey recovery code
  (`web/routes_security.py:630-661`, a plain `JSONResponse`, shown in an alert).
- Per-principal stores live in `paths.authority_dir(principal)` (local: `data_dir()/authority`;
  org: `data_dir()/users/<id>/authority`), written with `secure_files.atomic_write_json(path,
  data)` (`0600`, directory `0700`).

### Audit

- `AuditEntry` (`audit_log.py:130`ff), `CURRENT_SCHEMA_VERSION = 7`. Agent fields
  (`agent_id, agent_name, agent_version, agent_source`) are filled from `current_agent()`
  (`:595-600`). Decision values are listed in the `decision` field's comment (`:139-154`) and in
  the Excel `decision_fills` (`:793-815`). Settings mutations audit as `settings_change` through
  `_record_settings_audit` (`routes_settings.py:600-622`).

### Pattern commits and tests

- `78ccdd1b` moved `unattended_sessions.enabled` to `org_config.json` and added
  `build_org_bundle.py --enable-unattended-sessions`: the pattern for the new bundle keys.
- Tests to extend: `tests/unit/web/test_mcp_auth.py`, `tests/unit/web/test_routes_mcp.py`
  (`_OrgVerifier`, `TestListTools`, `TestCallConnectorTool`, `TestMetaTools`),
  `tests/unit/web/test_routes_mcp_agent_identity.py`, `tests/unit/web/test_mcp_dispatch.py`,
  `tests/unit/test_gate.py` (`TestUnattendedMode`, `TestPreflightAutoAccept`),
  `tests/unit/web/test_server.py` (`TestAudienceSeparation`, `TestAiClientTokenDelivery`),
  `tests/unit/web/test_routes_settings.py` (`TestAiClientTokenRoutes`,
  `TestBespokeRoutesAreClassified`), `tests/unit/web/test_routes_org_settings.py`,
  `tests/unit/test_settings_window_html.py`, `tests/unit/web/test_control_channel.py`
  (`TestCopyMcpCommand`), `tests/unit/web/test_oauth_provider.py`,
  `tests/unit/test_audit_log.py`, `tests/unit/test_daemon_main.py`.
- `tests/unit/test_code_no_history.py` rejects plan references in code.
- `tests/unit/test_website_docs_allowlist.py` fails on any `docs/*-plan.md`; the commit that adds
  this plan makes it skip `*-plan.md` (the same one-line change plan/data-sync carries, so the two
  merge cleanly).

## Design

### 3.1 The store: `src/privacyfence/scoped_tokens.py`

No web imports, so `gate.py` can import it.

```python
TOKEN_PREFIX = "pfst_"
DEFAULT_DAYS = 90
LOCAL_MAX_DAYS = 365
WARN_DAYS = 14
MAX_TOKENS_PER_PRINCIPAL = 50
LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}\Z")
STORE_FILE_NAME = "scoped_tokens.json"

@dataclass(frozen=True)
class ScopedToken:
    id: str                    # "st-" + 12 hex chars
    label: str
    digest: str                # sha256 hex of the full secret
    principal_id: str
    principal_email: str       # org mode, for the principal's claims; "" in local mode
    principal_display_name: str
    created_at: float
    expires_at: float
    rule_ids: frozenset[str]
    tools: frozenset[str]

    def days_left(self, now: float) -> int: ...      # ceil((expires_at - now) / 86400), floor 0
    def expired(self, now: float) -> bool: ...

@dataclass(frozen=True)
class ScopedTokenPolicy:
    enabled: bool
    max_days: int

class ScopedTokenError(ValueError): ...              # shown to the user verbatim; never carries a secret

def mint_secret() -> str: ...                        # TOKEN_PREFIX + secrets.token_urlsafe(32)
def policy_from_org_config(org_config: dict, *, org_mode: bool) -> ScopedTokenPolicy: ...

class ScopedTokenStore:
    def __init__(self, principal_dirs: Callable[[], Iterable[Principal]]) -> None: ...
    def load(self) -> None: ...
    def tokens_for(self, principal: Principal) -> list[ScopedToken]: ...
    def create(self, principal: Principal, *, label: str, days: int, rule_ids: Iterable[str],
               tools: Iterable[str], policy: ScopedTokenPolicy, now: float | None = None) -> tuple[ScopedToken, str]: ...
    def revoke(self, principal: Principal, token_id: str) -> bool: ...
    def revoke_all(self, principal_id: str) -> int: ...
    def find(self, secret: str, now: float | None = None) -> ScopedToken | None: ...
    def get(self, principal_id: str, token_id: str) -> ScopedToken | None: ...

def get_store() -> ScopedTokenStore: ...             # module singleton; reset in tests/conftest.py
def set_store(store: ScopedTokenStore | None) -> None: ...

_scope_ctx: ContextVar[ScopedToken | None]
def current_token_scope() -> ScopedToken | None: ...
class token_scope: ...                               # context manager setting _scope_ctx, like gate.unattended_scope
```

- **File**: `paths.authority_dir(principal) / STORE_FILE_NAME`, written with
  `secure_files.atomic_write_json(path, {"version": 1, "tokens": [...]})`. Each token is
  `{"id", "label", "digest", "principal_email", "principal_display_name", "created_at",
  "expires_at", "rule_ids": sorted, "tools": sorted}`. The secret itself is never stored; only its
  sha256 (ADR 0072's rule for secrets at rest). A malformed file or entry is skipped with a
  warning, never raised (fail closed: the token does not work).
- **`policy_from_org_config`**: local mode (`org_mode=False`) is `ScopedTokenPolicy(True,
  LOCAL_MAX_DAYS)` whatever the file says. Org mode reads `org_config["scoped_tokens"]`:
  `enabled` (bool, default `False`) and `max_days` (int 1-365, default `DEFAULT_DAYS`; out of
  range or not an int → `DEFAULT_DAYS` with a warning).
- **`create`** validates and raises `ScopedTokenError` with these exact texts:
  - disabled: `"Scoped tokens are not enabled on this install. Your administrator turns them on in the organization config bundle."`
  - label: `"A token name is 1 to 60 letters, digits, spaces, dots, dashes or underscores, starting with a letter or digit."`
  - duplicate label for the principal: `"You already have a token with that name."`
  - days outside 1..policy.max_days: `f"A token lasts 1 to {policy.max_days} days."`
  - no rule and no tool: `"Choose at least one tool for the token."` (a token with tools but no
    rules may still call `auto` tools)
  - unknown tool (not in `auto_accept.TOOL_TO_GATE`): `f"{name} is not a PrivacyFence tool."`
  - unknown rule id (not in `get_policy_v2_store_rules()` ids): `"One of the chosen rules no longer exists; reload the page."`
  - over `MAX_TOKENS_PER_PRINCIPAL`: `f"You have {MAX_TOKENS_PER_PRINCIPAL} tokens already; revoke one first."`

  It writes the file, updates the in-memory index and returns `(token, secret)`.
- **`find(secret)`**: returns `None` unless `secret` starts with `TOKEN_PREFIX`. Looks up
  `sha256(secret)` in the index (`digest -> (principal_id, token_id)`). Before trusting it, it
  `stat`s that principal's file and reloads it when `(st_mtime_ns, st_ino)` changed (the atomic
  replace gives a new inode, so a same-second edit on a filesystem with coarse timestamps is still
  seen), so a revocation written by
  another process (the admin command, §3.6) takes effect on the next call. Returns `None` for an
  expired token. Comparison is by dict lookup on the digest, never by comparing secrets.
- **Rules that change**: a token keeps rule ids. A rule removed from Settings, or replaced by one
  with different content (ADR 0074 ids are content-derived), simply stops matching; Settings shows
  such an id as "a rule that no longer exists".
- A threading lock guards the index and every write.

### 3.2 Authentication: `web/mcp_auth.py` and the wiring

```python
SCOPED_CLIENT_PREFIX = "scoped:"

class ScopedTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None: ...

class CompositeTokenVerifier(TokenVerifier):
    def __init__(self, *verifiers: TokenVerifier) -> None: ...
    async def verify_token(self, token: str) -> AccessToken | None: ...   # first non-None wins

def scope_from_access_token(token: AccessToken | None) -> ScopedToken | None: ...
```

- `ScopedTokenVerifier.verify_token` calls `scoped_tokens.get_store().find(token)` and, for a hit,
  returns `AccessToken(token=token, client_id=f"scoped:{t.id}", scopes=["scoped"],
  expires_at=int(t.expires_at), subject=t.principal_id, claims={"email": t.principal_email,
  "display_name": t.principal_display_name, "is_admin": False, "scoped_token_id": t.id})`.
- `CompositeTokenVerifier` tries `ScopedTokenVerifier` only for tokens starting with
  `TOKEN_PREFIX` and the wrapped verifier only for the others, so a scoped token is never probed
  against the OAuth store or the local token map and vice versa.
- `principal_from_access_token` gains a branch before the org branch: a `client_id` starting with
  `scoped:` whose `subject` is `LOCAL_PRINCIPAL.id` resolves to `LOCAL_PRINCIPAL`; otherwise the
  existing org branch builds the `Principal` from `subject` and claims (which works unchanged).
- `scope_from_access_token` returns `None` only for a token whose `client_id` does not start with
  `scoped:`. For a `scoped:` client id it returns `get_store().get(subject,
  claims["scoped_token_id"])`, read fresh on every call, or, when that record is gone (revoked
  between verification and use, or a file that no longer loads), the module constant
  `scoped_tokens.REVOKED_SCOPE`: a `ScopedToken(id="revoked", label="revoked token", digest="",
  principal_id="", principal_email="", principal_display_name="", created_at=0, expires_at=0,
  rule_ids=frozenset(), tools=frozenset())`. A missing record never falls back to the person's
  full access.
- `web/server.py`: local mode passes `CompositeTokenVerifier(ScopedTokenVerifier(),
  self.mcp_verifier)` to `mount_mcp`; where `build_app` builds a verifier from a bare token
  (`single_token_verifier`, the path at `:959-960`), it wraps that one the same way; org mode passes `CompositeTokenVerifier(ScopedTokenVerifier(),
  org.provider)`. `mount_file_bridge` keeps the plain `self.mcp_verifier`, so a scoped token is
  never accepted on the bearer file-bridge routes (only the shim uses them; the capability
  `slots`/`fetch` routes need no token). The store is created and `load()`ed at startup with a
  `principal_dirs` callable that yields `LOCAL_PRINCIPAL` plus `Principal(id=name)` for every
  directory `name` under `data_dir()/users/` that `paths.safe_principal_id` accepts. Unlike
  `mcp_auth.preload_verifier` (`:184-185`), it does not stop on an unseparated install: an org
  server is not privilege-separated, and its tokens must load after a restart.
- Org mode: `OrgOAuthProvider.revoke_all_for_principal(principal_id)` also calls
  `scoped_tokens.get_store().revoke_all(principal_id)` and adds its count to the return value, so
  the revoke-everything path, whenever an action calls it, takes the person's scoped tokens too.
- ADR 0061 holds: a scoped token is minted from `secrets`, never derived from a session or CSRF
  value, and is accepted only inside the `/mcp` stack.

### 3.3 Enforcement on a call

`web/routes_mcp.py`:

- `SCOPED_META_TOOLS = frozenset({"privacyfence_status", "privacyfence_check_policy",
  "privacyfence_create_upload_slot", "privacyfence_begin_unattended_session",
  "privacyfence_end_unattended_session"})`. A scoped token never gets
  `privacyfence_list_policy` (it would show the person's other rules), `privacyfence_await_approval`
  (nothing of its can be pending) or `privacyfence_propose_policy_change`.
- `handle_list_tools`: with `scope = scope_from_access_token(token)` not `None`, connector tools
  are kept only when `spec.name in scope.tools`, and meta tools only when in `SCOPED_META_TOOLS`.
  For `REVOKED_SCOPE` the list is empty.
- `handle_call_tool`: for `REVOKED_SCOPE` every call, meta tools included, returns
  `mcp_tools.error_result("Request denied: this token is no longer valid.")` after an audit row
  through `gate.audit_scoped_refusal`.
- `handle_call_tool`: with a scope, before dispatch, a `name` that is neither in `scope.tools` nor
  in `SCOPED_META_TOOLS` is refused: `gate.audit_scoped_refusal(connector, name, scope, summary)`
  writes the audit row (`connector` = the tool's connector or `"mcp"`, `decision=
  "denied_scoped_token"`, `summary=f"Tool not in scoped token {scope.label!r}"`), and the call
  returns `mcp_tools.error_result(f"Request denied: this token may not call {name}.")`.
  `audit_scoped_refusal(connector: str, tool: str, scope: ScopedToken, summary: str) -> None` is a
  new public function in `gate.py` that builds the row through the existing private `_audit`.
- `privacyfence_check_policy` under a scope: a `tool` argument not in `scope.tools` gets
  `ValueError(f"This token may not call {tool}.")`.
- The agent for a scoped call (meta tools included) is `AgentIdentity(id=f"scoped-token:{scope.id}",
  name=scope.label, version="", source=AgentSource.OVERRIDE)`: a credential a person created, so
  attested (ADR 0006's `override` row, reserved for this by ADR 0037). It replaces
  `_resolve_agent`'s result.
- Dispatch runs inside `scoped_tokens.token_scope(scope)` (entered next to `principal_scope`).
- `_dispatch_meta_tool` with a scope: `privacyfence_begin_unattended_session` returns
  `{"unattended": True, "scoped_token": True}` without the `unattended_sessions.enabled` check (a
  scoped call is always deny-only, which is all that flag does, ADR 0015);
  `privacyfence_end_unattended_session` returns the same dict (a scoped token cannot leave
  deny-only); `privacyfence_status` adds `"token": {"label", "expires_at": <ISO-8601 UTC>,
  "days_left", "expiring_soon": days_left <= WARN_DAYS, "rules": len(rule_ids), "tools":
  len(tools)}`, and when `expiring_soon` its `message` gains `f" This token expires in {n} day(s);
  ask its owner for a new one."`.

`src/privacyfence/gate.py`:

- `_evaluate_auto_accept` and `preflight_auto_accept`: with `scope = current_token_scope()` not
  `None`, the rule list is `[r for r in get_policy_v2_store_rules() if r.id in scope.rule_ids]`
  and the temp-accept window is not consulted (it was armed by a person's "Allow once" for their
  own session).
- Review gate (`:1086`) and popup gate (`:1219`): `if is_unattended():` becomes
  `if current_token_scope() is not None: _deny_scoped(...)` followed by the existing
  `elif is_unattended(): _deny_unattended(...)`.
- `_deny_scoped(audit, connector, tool, *, scope, pii_categories)` audits
  `decision="denied_scoped_token"`, `auto_accept_rule=""`, `pii_detected=bool(pii_categories)`,
  logs a warning naming the token id (never the secret) and raises `GateDeniedError("Request
  denied: this call uses a scoped token, and none of the token's rules covers it.")`.
- `propose_policy_change`: refused under a scope before the unattended check, with
  `GateDeniedError("Request denied: a scoped token cannot change PrivacyFence's rules.")`
  (defence in depth; the tool is not offered).

`web/mcp_dispatch.py`: `McpDispatcher._dedupe_key(principal_id, connector_name, tool, args)`
(`:298`) gains a `scope_id: str` parameter, and `call` passes
`(scoped_tokens.current_token_scope() or REVOKED_SCOPE).id if current_token_scope() else ""`, i.e.
the scope's id or `""` for an unscoped call. Without it a scoped call within 30 seconds of a
full-token call with the same arguments would get the full-token result, or its pending approval,
from the in-flight cache without passing the gate, and a full-token caller could get a scoped
call's denial.

`audit_log.py`: `denied_scoped_token` is added to the `decision` comment and to the Excel
`decision_fills` with the same fill as `denied_unattended`, and the summary sheet gains a row
`["Denied scoped token (outside its scope)", counts.get("denied_scoped_token", 0)]` after the
denied-unattended row (`:864`). No new field, no schema bump: the agent fields carry the token.

### 3.4 Settings, local mode

A new nav section **Scoped tokens** (`['scoped_tokens', 'Scoped tokens']`) after AI clients, shown
when `capabilities.sections.scoped_tokens` is true (local mode with an `/mcp` endpoint; org mode
per §3.5). Data comes in `window.__pfScopedTokens`, injected by `build_html(...,
scoped_tokens=...)` like `__pfAiClient`:

```json
{"delivery": "companion" | "once", "max_days": 365, "default_days": 90, "warn_days": 14,
 "tokens": [{"id", "label", "created_at", "expires_at", "days_left", "expiring_soon", "expired",
             "rules": [{"id", "sentence"} | {"id", "missing": true}], "tools": ["..."]}],
 "rules": [{"id", "sentence", "tools": ["..."]}],
 "tools": [{"connector", "tools": ["..."]}]}
```

`rules[].sentence` is `policy.describe.rule_sentence(rule)`; `rules[].tools` is
`describe.covered_tools(rule)`; `tools` groups `auto_accept.TOOL_TO_GATE` names by connector
prefix, enabled connectors only.

`renderScopedTokens()`:
- The list: one row per token with its label, "Expires 2026-12-30 (in 42 days)", a warning chip
  "Expires in N days" when `expiring_soon`, "Expired" when `expired`, its rules (sentences;
  "a rule that no longer exists" for a missing id) and tools in an expandable detail, and a
  **Revoke** button (`dataAttr('revoke_scoped_token', {token_id})`, class `button danger`).
- **Revoke all** (`revoke_all_scoped_tokens`), shown when there is at least one token.
- The **New token** form: name (text), days (number, default `default_days`, max `max_days`),
  rule checkboxes (sentence), tool checkboxes grouped by connector, and a link-style button
  **Add the tools these rules cover** that ticks every tool in the selected rules' `tools`.
  **Create token** posts `create_scoped_token`.
- After a successful create or revoke the page reloads (`location.reload()`), so the list is
  rebuilt from the server.

Routes (`web/routes_settings.py` `build_routes`, mounted only when the scoped-token wiring is
given, like `ai_client`), all with `_authenticated`, JSON body and `_check_mutation`:

- `POST /api/settings/scoped_tokens/create` `{label, days, rule_ids, tools, confirmed}`:
  `confirmed is True` (400 `"confirmation required"`), a human session where the install requires
  one (`_human_session_required_json("create a scoped token")`), the passkey step-up when
  `_step_up_in_force()` with `subject_key="create_scoped_token"` and
  `fingerprint=_action_fingerprint("create_scoped_token", {label, days, rule_ids sorted, tools
  sorted})`. Then `store.create(...)` (a `ScopedTokenError` → 400 `{"error": str(exc)}`), then
  `control_channel.send_mcp_token(secret)` to put it on the clipboard. When the companion cannot
  take it, the token is revoked again at once and the answer is 503 `{"error": f"The token was not
  created: {reason}"}`. Success answers `{"created": {"id", "label", "expires_at"}}` and never the
  secret.
- `POST /api/settings/scoped_tokens/revoke` `{token_id, confirmed}`: confirmation, human session,
  passkey step-up (`subject_key="revoke_scoped_token"`, fingerprint over `{token_id}`), then
  `store.revoke`. 404 `{"error": "No such token."}` when it does not exist.
- `POST /api/settings/scoped_tokens/revoke_all` `{confirmed}`: the same gates
  (`subject_key="revoke_all_scoped_tokens"`, fingerprint over `{}`), then `store.revoke_all`.
- All three go into `_BESPOKE_SENSITIVE_ROUTE_PATHS`. Each audits through `_record_settings_audit`
  with a summary that names the token's label and id, never the secret: `"Created scoped token
  'sales-sync' (st-…), 90 days, 2 rule(s), 5 tool(s)"`, `"Revoked scoped token 'sales-sync'
  (st-…)"`, `"Revoked all scoped tokens (3)"`.
- The bridge shim maps the three actions to these routes and asks `window.confirm` before each,
  with the texts `"Create this token? It is copied to your clipboard once and cannot be shown
  again."` (local) or `"Create this token? It is shown once and cannot be shown again."` (org),
  `"Revoke this token? Clients using it stop working at once."` and `"Revoke all your scoped
  tokens?"`. These routes answer with no page state, so the shim gets its own success branch for
  them: when the response has `secret` (org), it first shows the one-time dialog (§3.5) and
  reloads when the dialog closes; otherwise it calls `location.reload()` at once. It never calls
  `window.__pfRender` for them. Errors use the existing error display.
- `control_channel._MCP_TOKEN_PATTERN` already accepts `pfst_` + 43 URL-safe characters (48 in
  all); `TestCopyMcpCommand` gains a test pinning that.

### 3.5 Settings, organization mode

- `org_config.json` gains `scoped_tokens: {enabled: bool, max_days: int}`.
  `scripts/build_org_bundle.py` gains `--enable-scoped-tokens` / `--disable-scoped-tokens`
  (mutually exclusive, not written by default) and `--scoped-token-max-days N` (1-365), copying
  the `--enable-unattended-sessions` pattern (`78ccdd1b`). The policy is
  `policy_from_org_config(org_config, org_mode=True)`.
- `_capabilities_for` sets `sections.scoped_tokens` to the policy's `enabled` in org mode. The
  same `renderScopedTokens()` renders it with `delivery: "once"` and the policy's `max_days`.
- `build_org_routes` adds the same three routes, as bespoke org routes (as `org_config_upload` is
  one) with the org gates: `org_session.authenticated`, `org_session.check_csrf`, `check_origin`,
  the policy's `enabled` (403 with the disabled text from §3.1), and, when `step_up is not None
  and step_up.enabled and step_up.require_passkey` (org mode has no `_step_up_in_force`), the
  passkey step-up through `_verify_or_challenge` with the org `step_up` and `step_up_origin`,
  under the same `subject_key`s and fingerprints. Inside `principal_scope(principal)`, each route
  first calls `_ensure_principal_settings_loaded()` (`routes_settings.py:1267`), as
  `settings_action` does, so the principal's rules are loaded before `create` validates rule ids.
- Delivery: an org server has no companion on the person's computer, so the create route answers
  `{"created": {...}, "secret": "<token>"}` once, with `Cache-Control: no-store`. The page shows
  it in a dialog with the token in a read-only field, a **Copy** button
  (`navigator.clipboard.writeText` inside the click handler, falling back to selecting the field)
  and the text `"Copy this token now. It is not stored and cannot be shown again."`; the dialog
  has no other way to bring the secret back. The secret is never logged or audited.
- The routes run inside `principal_scope(principal)`, and `create` stores the principal's email
  and display name for the token's claims.

### 3.6 The administrator's revoke command

`privacyfence-app --revoke-scoped-tokens PRINCIPAL_ID` (in `daemon_main.py`): dispatched after
`privilege_separation.check_runtime_identity()` (`:2308`), not beside `--print-mcp-token`, which
deliberately runs before it as the human and only talks over the control channel. On a separated
install that check already refuses the wrong account with its own message. It checks that
`PRINCIPAL_ID` passes `paths.safe_principal_id` and that it is `local` or an existing
`data_dir()/users/<id>` directory before touching `authority_dir()` (which would create the
folder); otherwise it prints `"No such account."` and exits 1. Then it builds a
`ScopedTokenStore`, calls `revoke_all(PRINCIPAL_ID)`, prints `f"Revoked {n} scoped token(s) for
{PRINCIPAL_ID}."` and exits 0. The running daemon picks the change up on the token's next use (§3.1, `find`'s mtime
check). In local mode `PRINCIPAL_ID` is `local` or `os-<uid>`; in org mode it is the principal id
the audit log and the org Settings page show.

### 3.7 Rejected alternatives

- **A narrowed OAuth grant in organization mode** (a scope string in the authorization request,
  consent on the sign-in page, enforcement from `AccessToken.scopes`). The SDK's DCR flow has no
  scope negotiation PrivacyFence uses today, the refresh chain ends after 30 days (ADR 0072), which
  cannot carry a 90-day job, and every scheduled run would depend on a refresh token kept by the
  client. A bearer token created on the Settings page, behind the passkey and an administrator's
  opt-in, is simpler and revocable in one place.
- **Showing the local token in the response, behind a passkey.** ADR 0104 rejected it for the main
  token: any process running as the user can already reach the companion, so the passkey adds
  nothing, and the secret would sit in an HTTP response. Local scoped tokens use the companion the
  same way.
- **Storing the secret** (or encrypting it) so it can be shown again. A hash is enough to verify a
  bearer token; anyone who loses it creates a new one.
- **Scoping by tools only, or rules only.** Rules decide what auto-accepts; tools decide what the
  client can see and call, including `auto` tools no rule governs. Both are needed to make a job's
  reach exactly its recipe's.
- **Letting a scoped token ask for approval.** A scheduled job has nobody to answer, and a card
  from a narrow credential would invite approving things outside its purpose.
- **A new audit field for the token.** The agent fields already say who made a call; recording the
  token as an attested agent fills them truthfully (ADR 0006, ADR 0037).
- **Accepting scoped tokens on the bearer file-bridge routes.** Only the `.mcpb` shim uses them,
  and the shim always holds the main token.

## ADRs

- **0164** — Scoped tokens: named bearer tokens for `/mcp` in both modes, each with an expiry
  (90 days by default), an allowlist of rules and of tools, deny-only (never a card or a pending
  approval), stored as a hash in the person's `authority/` folder, recorded as the attested agent
  `scoped-token:<id>`, which outranks an org admin pin for that call. Amends ADR 0037 (puts
  `AgentSource.OVERRIDE` to the per-credential use it reserved), ADR 0006 (decision 2's ranking
  gains the token identity first), ADR 0008's "one token per OS account" (a person can now hold
  scoped tokens too) and ADR 0015 (`privacyfence_begin_unattended_session` succeeds for a scoped
  token without the admin opt-in, because a scoped call is deny-only already).
- **0165** — In organization mode scoped tokens are an administrator opt-in
  (`scoped_tokens.enabled`, `scoped_tokens.max_days`), delivered once in the creating response
  rather than as a narrowed OAuth grant, and revoked with everything else by the provider's
  revoke-all path and by `privacyfence-app --revoke-scoped-tokens`. Amends ADR 0072 (org mode now
  also keeps hashed scoped tokens at rest). It states the trade-off the user accepted: an OAuth
  grant ends within its 30-day refresh chain, while a scoped token lives until it expires (90 days
  by default, at most the administrator's cap) unless it is revoked, so offboarding a person
  includes running the revoke command; org mode has no automatic "sign out everywhere" today.

If a parallel branch takes these numbers first (the data-sync plan uses 0157-0163), renumber in
order before merging (ADR README rule 6).

## Manual steps

Nothing is needed before implementation. After the last merge, before the PR merges
([step-by-step page](https://claude.ai/artifact/1jvzy3Xri9rK5yvhPhTBnR)):

- **ma1** — On a local install, create a scoped token in Settings, connect a client with it, and
  check the filtered tool list, a denied out-of-scope call, the audit rows, the expiry display and
  revocation.
- **ma2** — On an organization test server, enable scoped tokens in the bundle, create a token,
  use it as a bearer token, and revoke it with `privacyfence-app --revoke-scoped-tokens`.

## Risks and open questions

- **Overlap with plan/data-sync** (not merged): both change `src/privacyfence/gate.py` (data-sync
  adds a personal-data block right after the `try:` in `gated_call` and a `content_reaches_result`
  parameter; this plan changes `_evaluate_auto_accept`, `preflight_auto_accept` and the two
  `is_unattended()` sites), `src/privacyfence/settings_window_html.py` and
  `src/privacyfence/web/routes_settings.py` (data-sync adds the personal-data checkbox to the
  Auto-accept form; this plan adds a nav section and routes), and both take ADR numbers. Whichever
  merges second resolves the conflicts: the data-sync personal-data rule must also be filtered by
  the token's rule ids (`_personal_data_rule` reads `get_policy_v2_store_rules()`; under a scope it
  must use the same filtered list). `sync/README.md` (data-sync) should gain a "Scoped tokens"
  paragraph and `privacyfence-sync` could print the `privacyfence_status` expiry warning at the
  start of a run; both are follow-ups for whichever lands second, not phases here.
- **The SDK's `AccessToken` model** may refuse an unknown key in `claims` or a non-list `scopes`.
  If constructing it as §3.2 says raises, stop with `status=blocked` and quote the error.
- **`_MCP_TOKEN_PATTERN`** must accept the new secret. If `pfst_` + `token_urlsafe(32)` ever fails
  the pattern (it should not: 48 characters of `[A-Za-z0-9_-]`), stop with `status=blocked`.
- **Step-up follows the existing rule.** Creation and revocation take the passkey whenever the
  install requires one for sensitive actions (`step_up.enabled and step_up.require_passkey`), like
  rotating the local token; where an install does not require it, they take the confirmation and
  the human-session check only. The phases do not invent a stricter rule.
- **Line numbers drift.** Everything here was read on `5041f0bc`. A worker finds the named symbol;
  if a named symbol does not exist, it stops.

## Implementation manifest

```yaml
plan_slug: scoped-tokens
feature_branch: feature/scoped-tokens
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/1jvzy3Xri9rK5yvhPhTBnR
manual_steps_source: docs/scoped-tokens-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-local-scoped-token
    title: Create, use and revoke a scoped token on a local install
    why: Only a real daemon, companion clipboard and MCP client show the end-to-end flow; CI fakes the companion and the client.
  - id: ma2-org-scoped-token
    title: Enable, create, use and revoke a scoped token on an organization test server
    why: The bundle opt-in, the one-time reveal behind a reverse proxy, the passkey with the org rp_id and the admin revoke command need a real org server.
verify_after_merge:
  - python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/web/test_mcp_auth.py tests/unit/web/test_routes_mcp.py tests/unit/web/test_routes_mcp_agent_identity.py tests/unit/web/test_mcp_dispatch.py tests/unit/test_gate.py -q
  - python3 -m pytest tests/unit/web/test_server.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/test_settings_window_html.py tests/unit/web/test_control_channel.py tests/unit/web/test_oauth_provider.py tests/unit/test_audit_log.py tests/unit/test_daemon_main.py -q
final_checks:
  - docs/scoped-tokens-plan.md and docs/scoped-tokens-plan-manual-steps.html are deleted and nothing links to them (git grep -n "scoped-tokens-plan" prints nothing)
  - ADRs 0164 and 0165 (or the renumbered pair) exist in docs/adr/, have Status Accepted and are listed in docs/adr/README.md; ADRs 0006, 0008, 0015 and 0037 have an "Amended by 0164" Status line and ADR 0072 an "Amended by 0165" one
  - CHANGELOG.md has the new entries under "## [Unreleased]" and no new version heading
  - pytest tests/integration -v passes locally, including test_mcp_daemon_contract.py and test_shim_mcp_contract.py (the auth stack changed)
  - Only if `git grep -n "def _personal_data_rule" origin/main -- src/privacyfence/gate.py` matches (the data-sync work merged first), the feature branch has merged origin/main, `_personal_data_rule` reads the token-filtered rule list under a scope (the same filter `_evaluate_auto_accept` uses), and a test in tests/unit/test_gate.py shows a scoped call with personal data denied when the only flagged rule is outside the token's scope
phases:
  - id: p1-store
    title: The scoped-token store, policy and request scope (scoped_tokens.py)
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - tests/unit/test_scoped_tokens.py
      - tests/conftest.py
    brief: |
      Read docs/coding-and-testing-guidelines.md (§1.5, §1.6, §2.3), docs/adr/0072, 0074 and 0104,
      and Design §3.1 of docs/scoped-tokens-plan.md. No comment or docstring may cite the plan;
      comments may cite ADR 0164.
      1. Create src/privacyfence/scoped_tokens.py exactly as §3.1: constants, ScopedToken,
         ScopedTokenPolicy, ScopedTokenError (with the listed texts), mint_secret,
         policy_from_org_config, ScopedTokenStore, get_store/set_store, REVOKED_SCOPE (§3.2), the
         context variable, current_token_scope and token_scope. Use paths.authority_dir, secure_files.atomic_write_json,
         hashlib.sha256, secrets, threading.Lock. Rule ids are validated against
         auto_accept.get_policy_v2_store_rules(); tools against auto_accept.TOOL_TO_GATE.
      2. tests/conftest.py: add `scoped_tokens.set_store(None)` to `_reset()` with a one-line
         comment, like the other singleton resets.
      3. tests/unit/test_scoped_tokens.py (pytest, tmp_path, monkeypatch paths.data_dir; no
         network): one test per validation text in §3.1;
         test_create_returns_a_prefixed_secret_and_stores_only_its_digest (the file has no
         secret); test_file_is_mode_600 (skip on Windows with a reason);
         test_find_returns_the_token_and_none_for_another_secret;
         test_find_ignores_secrets_without_the_prefix; test_find_returns_none_once_expired;
         test_revoke_and_revoke_all; test_a_revocation_written_by_another_store_is_seen_on_next_find
         (two store instances over one data dir; revoke with the second, find with the first);
         test_malformed_file_is_skipped_not_raised; test_policy_local_is_always_enabled_365;
         test_policy_org_defaults_off_90_and_clamps_bad_max_days; test_days_left_and_expired;
         test_token_scope_sets_and_resets_the_context.
      4. `ruff check .`, `python3 -m mypy src/privacyfence`, `bandit -c pyproject.toml -r src`,
         `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q passes
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q --cov=privacyfence.scoped_tokens --cov-branch --cov-report=term-missing reports 100% for scoped_tokens.py
      - grep -n "scoped_tokens.set_store(None)" tests/conftest.py matches
      - python3 -m pytest tests/unit -q passes

  - id: p2-auth
    title: Accept scoped tokens on /mcp in both modes (verifiers, principal, wiring, revoke-all hook)
    depends_on: [p1-store]
    complexity: M
    worker_model: opus
    worker_model_reason: It changes the /mcp authentication stack in both modes; a mistake accepts a token where it must not (ADR 0061's audience separation, the file bridge, the OAuth store), which needs judgement beyond following steps.
    touches:
      - src/privacyfence/web/mcp_auth.py
      - src/privacyfence/web/server.py
      - src/privacyfence/web/oauth_provider.py
      - tests/unit/web/test_mcp_auth.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_oauth_provider.py
    brief: |
      Read docs/adr/0006, 0008, 0037, 0061 and 0072 and Design §3.2 of docs/scoped-tokens-plan.md.
      No comment or docstring may cite the plan; comments may cite ADR 0164 and 0165.
      1. src/privacyfence/web/mcp_auth.py: SCOPED_CLIENT_PREFIX, ScopedTokenVerifier,
         CompositeTokenVerifier (prefix routing as §3.2), scope_from_access_token, and the new
         branch in principal_from_access_token.
      2. src/privacyfence/web/server.py: create and load the store at startup (the principal walk
         of mcp_auth.preload_verifier), call scoped_tokens.set_store, and pass the composite
         verifier to mount_mcp in local and org mode (and around single_token_verifier in
         build_app's bare-token path); leave mount_file_bridge on the plain self.mcp_verifier. The
         principal walk is §3.2's, not preload_verifier's.
      3. src/privacyfence/web/oauth_provider.py: revoke_all_for_principal also revokes the
         principal's scoped tokens and adds the count.
      4. Tests:
         - tests/unit/web/test_mcp_auth.py: TestScopedTokenVerifier
           (test_a_stored_token_verifies_with_its_scoped_client_id_subject_and_expiry,
           test_an_unknown_or_revoked_token_does_not_verify, test_an_expired_token_does_not_verify);
           TestCompositeTokenVerifier (test_a_prefixed_token_never_reaches_the_inner_verifier,
           test_an_unprefixed_token_never_reaches_the_scoped_store);
           TestPrincipalFromAccessToken: test_a_local_scoped_token_resolves_to_the_local_principal,
           test_an_org_scoped_token_resolves_to_its_owner_with_claims;
           test_scope_from_access_token_returns_revoked_scope_when_the_record_is_gone (a revocation
           between two calls gives REVOKED_SCOPE, never None), test_an_unscoped_token_has_no_scope;

         - tests/unit/web/test_server.py: test_org_tokens_load_after_a_restart_on_an_unseparated_install
           (privilege_separation disabled, a token file under data_dir()/users/<id>/authority, a
           fresh store load finds it). TestAudienceSeparation: test_a_scoped_token_is_refused_on_settings_and_approvals,
           test_a_scoped_token_is_refused_on_the_bearer_file_bridge, and
           test_a_scoped_token_is_accepted_on_mcp (local and org app builds).
         - tests/unit/web/test_oauth_provider.py: test_revoke_all_for_principal_revokes_scoped_tokens_too.
      5. Run `pytest tests/integration -v` (test_mcp_daemon_contract.py and
         test_shim_mcp_contract.py must pass: the profile's conditional row for web/server.py and
         web/mcp_auth.py). `ruff check .`, `python3 -m mypy src/privacyfence`,
         `python3 scripts/mypy_strict_modules.py`, `bandit -c pyproject.toml -r src`,
         `python3 -m pytest tests/unit -q`.
      Stop condition: if constructing AccessToken with the §3.2 claims and scopes raises, stop
      with status=blocked and quote the error.
    acceptance:
      - python3 -m pytest tests/unit/web/test_mcp_auth.py tests/unit/web/test_server.py tests/unit/web/test_oauth_provider.py -q passes
      - python3 -m pytest tests/unit/web/test_server.py -q -k "scoped_token" passes and runs at least 3 tests
      - pytest tests/integration -v passes
      - python3 -m pytest tests/unit -q passes

  - id: p3-enforce
    title: Filter tools, refuse out-of-scope calls, attest the token, deny instead of prompting
    depends_on: [p2-auth]
    complexity: M
    worker_model: opus
    worker_model_reason: The gate is a security boundary (guidelines §1.5); the change must narrow what auto-accepts under a scope without widening anything for unscoped calls, and every branch must audit.
    touches:
      - src/privacyfence/web/routes_mcp.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/gate.py
      - src/privacyfence/audit_log.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_routes_mcp.py
      - tests/unit/web/test_routes_mcp_agent_identity.py
      - tests/unit/test_gate.py
      - tests/unit/test_audit_log.py
    brief: |
      Read docs/coding-and-testing-guidelines.md §1.5, docs/adr/0006, 0015 and 0037, and Design
      §3.3 of docs/scoped-tokens-plan.md. No comment or docstring may cite the plan; comments may
      cite ADR 0164.
      1. src/privacyfence/web/routes_mcp.py: SCOPED_META_TOOLS; the list filter in
         handle_list_tools; the out-of-scope refusal with its audit row in handle_call_tool; the
         scoped agent identity; dispatch inside scoped_tokens.token_scope(scope); the three
         _dispatch_meta_tool behaviours (begin/end unattended, status token block and message,
         check_policy's tool restriction); REVOKED_SCOPE handling in both handlers.
      1b. src/privacyfence/web/mcp_dispatch.py: the scope id in _dedupe_key and its caller, as §3.3.
      2. src/privacyfence/gate.py: the rule filter and no temp-accept under a scope in
         _evaluate_auto_accept and preflight_auto_accept; _deny_scoped; audit_scoped_refusal; the
         two gate sites; the propose_policy_change refusal, all as §3.3.
      3. src/privacyfence/audit_log.py: denied_scoped_token in the decision comment, the Excel
         decision_fills and the summary sheet row.
      4. Tests:
         - tests/unit/web/test_routes_mcp.py, a new class TestScopedTokens using the file's
           _OrgVerifier pattern with a verifier that returns a scoped AccessToken and a store with
           one token: test_tools_list_shows_only_scoped_tools_and_meta_tools,
           test_an_out_of_scope_tool_is_refused_and_audited, test_status_reports_the_token_and_its_expiry,
           test_status_warns_when_the_token_expires_soon, test_begin_unattended_needs_no_admin_opt_in,
           test_list_policy_and_propose_are_not_offered_and_are_refused,
           test_a_revoked_scope_lists_nothing_and_refuses_every_call,
           test_check_policy_refuses_a_tool_outside_the_scope.
         - tests/unit/web/test_mcp_dispatch.py TestCall: test_a_scoped_call_never_reuses_an_unscoped_result
           (a full-token call, then the same call under token_scope within the dedupe window: the
           connector is called twice) and test_an_unscoped_call_never_reuses_a_scoped_denial.
         - tests/unit/web/test_routes_mcp_agent_identity.py: test_a_scoped_call_is_attested_as_the_token
           (agent_source "override", agent_id "scoped-token:<id>", agent_name the label).
         - tests/unit/test_gate.py, a new class TestScopedTokenGate next to TestUnattendedMode:
           test_only_the_tokens_rules_auto_accept, test_a_matching_rule_outside_the_scope_is_denied,
           test_no_card_and_no_pending_for_a_scoped_call (review and popup gates),
           test_temp_accept_does_not_apply_under_a_scope, test_pii_on_a_scoped_call_is_denied,
           test_preflight_reports_only_the_tokens_rules, test_propose_is_refused_under_a_scope,
           test_unscoped_calls_are_unchanged (pins today's behaviour for one review and one popup
           case).
         - tests/unit/test_audit_log.py: the Excel export gives denied_scoped_token the
           denied_unattended fill and counts it on the summary sheet.
      5. `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 scripts/mypy_strict_modules.py`,
         `bandit -c pyproject.toml -r src`, `python3 -m pytest tests/unit -q`, and
         `pytest tests/integration -v` (routes_mcp.py changed).
      Stop condition: if gated_call's review or popup branch has no `if is_unattended():` site
      where §3.3 says, stop with status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_mcp.py tests/unit/web/test_routes_mcp_agent_identity.py tests/unit/web/test_mcp_dispatch.py tests/unit/test_gate.py tests/unit/test_audit_log.py -q passes
      - python3 -m pytest tests/unit/test_gate.py -q -k "ScopedTokenGate" passes and runs at least 8 tests
      - python3 -m pytest tests/unit/web/test_mcp_dispatch.py -q -k "scoped" passes and runs at least 2 tests
      - grep -n '"denied_scoped_token"' src/privacyfence/gate.py src/privacyfence/web/routes_mcp.py matches in both files
      - pytest tests/integration -v passes
      - python3 -m pytest tests/unit -q passes

  - id: p4a-local-routes
    title: Local-mode scoped-token routes, wiring and companion delivery
    depends_on: [p3-enforce]
    complexity: M
    touches:
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/server.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_control_channel.py
    brief: |
      Read docs/adr/0104 and Design §3.4 of docs/scoped-tokens-plan.md (routes, gates, audit
      summaries, shim). Copy the shape of the existing Copy token / Rotate token routes
      (routes_settings.py mcp_token_copy and mcp_token_rotate, server.py copy_local_mcp_token and
      the AiClientConnect wiring). No comment or docstring may cite the plan.
      1. src/privacyfence/web/routes_settings.py: a `ScopedTokenWiring` dataclass (`store_create`,
         `store_revoke`, `store_revoke_all`, `send_secret`, `page_data`, all callables) passed to
         build_routes like ai_client; the three routes with the gates, texts and audit summaries of
         §3.4; their paths in _BESPOKE_SENSITIVE_ROUTE_PATHS; the bridge shim mappings, confirm
         texts and the reload-on-success branch (no window.__pfRender for these routes); build_html
         receives `scoped_tokens=wiring.page_data()` (the HTML side comes in p4b; until then
         build_html accepts and ignores the argument with a default of None).
      2. src/privacyfence/web/server.py: build the wiring in local mode with an /mcp endpoint,
         using the store, `policy_from_org_config(..., org_mode=False)`,
         control_channel.send_mcp_token and the page data of §3.4 (rules with sentences and covered
         tools, tools grouped by connector, tokens with days_left/expiring_soon/expired and missing
         rule ids), under principal_scope(LOCAL_PRINCIPAL) as copy_local_mcp_token does.
      3. Tests:
         - tests/unit/web/test_routes_settings.py, a new class TestScopedTokenRoutes modelled on
           TestAiClientTokenRoutes: test_create_copies_through_the_companion_and_returns_no_secret,
           test_create_needs_confirmation_a_human_session_and_the_passkey,
           test_a_create_the_companion_could_not_take_is_undone, test_create_shows_the_validation_text,
           test_revoke_and_revoke_all_take_the_same_gates, test_revoke_of_an_unknown_token_is_404,
           test_audit_summaries_name_the_token_and_never_the_secret,
           test_the_shim_reloads_after_these_routes_instead_of_rendering_state.
           TestBespokeRoutesAreClassified keeps passing with the three paths listed as sensitive.
         - tests/unit/web/test_server.py: test_the_scoped_token_wiring_is_built_only_with_an_mcp_endpoint,
           test_page_data_marks_expiring_expired_and_missing_rules.
         - tests/unit/web/test_control_channel.py TestCopyMcpCommand:
           test_the_pattern_accepts_a_scoped_token_secret (scoped_tokens.mint_secret()).
      4. `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_server.py tests/unit/web/test_control_channel.py -q passes
      - grep -n "/api/settings/scoped_tokens/create" src/privacyfence/web/routes_settings.py matches at least twice
      - python3 -m pytest tests/unit -q passes

  - id: p4b-local-section
    title: The Scoped tokens Settings section (list, revoke, new-token form)
    depends_on: [p4a-local-routes]
    complexity: S
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read docs/coding-and-testing-guidelines.md §1.10 (design system) and Design §3.4 of
      docs/scoped-tokens-plan.md (the section and the page data). No comment or docstring may cite
      the plan.
      1. src/privacyfence/settings_window_html.py: the nav entry, the capability, the
         window.__pfScopedTokens injection in build_html, and renderScopedTokens() with the list
         (expiry text, "Expires in N days" chip, "Expired", rules and tools detail, Revoke), Revoke
         all, and the New token form with "Add the tools these rules cover", as §3.4.
      2. Tests in tests/unit/test_settings_window_html.py, a new class TestScopedTokensSection:
         test_local_mode_with_an_mcp_url_shows_the_section, test_expiring_and_expired_tokens_are_flagged,
         test_the_form_offers_rules_tools_and_the_add_covered_tools_button,
         test_a_missing_rule_is_shown_as_no_longer_existing.
      3. Run tests/integration/test_browser_smoke.py with PRIVACYFENCE_TEST_CHROMIUM set
         (.claude/skills/steward/SKILL.md); a SKIPPED run is not a pass.
      4. `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/unit/test_settings_window_html.py -q -k "ScopedTokensSection" passes and runs at least 4 tests
      - python3 -m pytest tests/unit -q passes

  - id: p5-org-settings
    title: Organization mode opt-in, one-time reveal, and the administrator's revoke command
    depends_on: [p4b-local-section]
    complexity: M
    touches:
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/web/server.py
      - src/privacyfence/daemon_main.py
      - scripts/build_org_bundle.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_build_org_bundle.py
    brief: |
      Read docs/adr/0015 and 0104, docs/org-mode-setup-guide.md's bundle flags section, commit
      78ccdd1b (the --enable-unattended-sessions pattern), and Design §3.5 and §3.6 of
      docs/scoped-tokens-plan.md. No comment or docstring may cite the plan; comments may cite
      ADR 0165.
      1. scripts/build_org_bundle.py: --enable-scoped-tokens / --disable-scoped-tokens and
         --scoped-token-max-days, writing org_config["scoped_tokens"] as 78ccdd1b writes
         unattended_sessions.
      2. src/privacyfence/web/routes_settings.py build_org_routes: the three routes with the org
         gates of §3.5 (step-up when `step_up is not None and step_up.enabled and
         step_up.require_passkey`), `_ensure_principal_settings_loaded()` inside
         principal_scope before create, the disabled 403, and the one-time secret in the create
         response with Cache-Control: no-store.
      3. src/privacyfence/web/server.py: pass the org policy and the store to build_org_routes.
      4. src/privacyfence/settings_window_html.py: the org capability from the policy, and the
         "once" delivery dialog with Copy and its text.
      5. src/privacyfence/daemon_main.py: --revoke-scoped-tokens PRINCIPAL_ID as §3.6, dispatched
         after privilege_separation.check_runtime_identity().
      6. Tests:
         - tests/unit/web/test_routes_org_settings.py: test_scoped_tokens_are_refused_when_the_admin_has_not_enabled_them,
           test_create_returns_the_secret_once_with_no_store, test_create_takes_the_org_step_up,
           test_the_created_token_verifies_on_mcp_as_its_owner, test_revoke_and_revoke_all,
           test_days_over_the_admin_cap_are_refused.
         - tests/unit/test_settings_window_html.py TestScopedTokensSection:
           test_org_mode_shows_the_section_only_when_enabled, test_org_mode_uses_the_one_time_dialog.
         - tests/unit/test_daemon_main.py: test_revoke_scoped_tokens_revokes_and_reports_the_count,
           test_revoke_scoped_tokens_for_an_unknown_account_exits_1_and_creates_no_folder,
           test_revoke_scoped_tokens_runs_after_the_runtime_identity_check.
         - tests/unit/test_build_org_bundle.py: test_enable_and_disable_scoped_tokens_flags,
           test_scoped_token_max_days_range.
      7. Run tests/integration/test_browser_smoke.py with PRIVACYFENCE_TEST_CHROMIUM set; a
         SKIPPED run is not a pass. `ruff check .`, `python3 -m mypy src/privacyfence`,
         `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_org_settings.py tests/unit/test_settings_window_html.py tests/unit/test_daemon_main.py tests/unit/test_build_org_bundle.py -q passes
      - python3 -m pytest tests/unit -q -k "scoped_token or scoped_tokens" passes
      - grep -n "enable-scoped-tokens" scripts/build_org_bundle.py matches
      - python3 -m pytest tests/unit -q passes

  - id: p6-retire
    title: ADRs 0164 and 0165, reference docs, changelog, and retire the plan
    depends_on: [p5-org-settings]
    complexity: S
    touches:
      - docs/adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md
      - docs/adr/0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md
      - docs/adr/0037-a-local-override-is-a-relabel-and-never-attests.md
      - docs/adr/0008-one-principal-per-os-user.md
      - docs/adr/0006-attributing-a-request-to-the-ai-system-that-made-it.md
      - docs/adr/0015-unattended-session-flag-is-advisory-only.md
      - docs/adr/0072-org-mode-persists-only-sealed-refresh-tokens.md
      - docs/adr/README.md
      - docs/connect-claude-code.md
      - docs/security-and-compliance.md
      - docs/how-it-works.md
      - docs/configuration-reference.md
      - docs/org-mode-setup-guide.md
      - CHANGELOG.md
      - docs/scoped-tokens-plan.md
      - docs/scoped-tokens-plan-manual-steps.html
    brief: |
      Read docs/adr/README.md (template and rules) and docs/coding-and-testing-guidelines.md §5.
      1. Write ADR 0164 and ADR 0165 from the plan's ADRs section and Design §3.1-§3.7 (Context,
         Decision, Alternatives considered from §3.7, Consequences, Verification naming the test
         classes, Related; never this plan). Status "Accepted — <today>. Implemented." If 0164 or
         0165 is taken on main, renumber in order and fix every comment that cites them
         (git grep -n "ADR 016[45]" src scripts).
      2. One Status line each and nothing else: docs/adr/0037, 0008, 0006 and 0015 get "Amended by
         [ADR 0164](0164-...md).", docs/adr/0072 gets "Amended by [ADR 0165](0165-...md).".
      3. docs/adr/README.md: two index rows.
      4. Reference docs (today's behaviour only, §5):
         - docs/connect-claude-code.md: a "Scoped tokens" subsection under both modes (create in
           Settings, use as the bearer token, expiry, revoke).
         - docs/security-and-compliance.md: a "## Scoped tokens" section after "## Recovery code"
           (what a scope limits, deny-only, stored as a hash, attested agent, one-time reveal in
           org mode, admin opt-in and revoke command); scoped_tokens.json in the authority/
           table; and fix the sentence that says every AI client of one OS account holds the same
           MCP token.
         - docs/how-it-works.md: a "### Scoped tokens" under "## How an AI system connects", and a
           sentence in "## Which AI system is asking" about the attested token name.
         - docs/configuration-reference.md and docs/org-mode-setup-guide.md: the two bundle flags
           and the scoped_tokens keys.
      5. CHANGELOG.md under "## [Unreleased]": one line for scoped tokens (both modes) and one
         for --revoke-scoped-tokens.
      6. Delete docs/scoped-tokens-plan.md and docs/scoped-tokens-plan-manual-steps.html;
         `git grep -n "scoped-tokens-plan"` prints nothing.
      7. `python3 -m pytest tests/unit -q` and `ruff check .`.
    acceptance:
      - ls docs/adr/0164-*.md docs/adr/0165-*.md succeeds (or the renumbered pair)
      - grep -l "Amended by \[ADR 0164\]" docs/adr/0006-*.md docs/adr/0008-*.md docs/adr/0015-*.md docs/adr/0037-*.md lists all four files
      - grep -c "Amended by \[ADR 0165\]" docs/adr/0072-org-mode-persists-only-sealed-refresh-tokens.md prints 1
      - git grep -n "scoped-tokens-plan" prints nothing
      - python3 -m pytest tests/unit -q passes
```
