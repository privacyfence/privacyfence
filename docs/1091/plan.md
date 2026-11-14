# Upstream MCP connector: tool policy in PrivacyFence, signed caller claims to the upstream

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator of an organization server writes a small JSON file that names a remote MCP
server (for example a knowledge hub), lists which of its tools PrivacyFence exposes and how each is
gated, and points `PRIVACYFENCE_UPSTREAM_MCP_FILE` at it. An administrator adds domain grants to
`admin_config.json`. AI clients then see tools such as `kh_search` and `kh_propose_change`: reads
declared `auto` run at once, `popup` writes show an approval card, and every forwarded call carries a
60-second PrivacyFence-signed JWT naming the person, their allowed domains and the calling client or
workflow. The upstream verifies it against `/upstream-mcp/jwks` and filters its own queries. A scoped
token gets only the domains its creator ticks.

**Scope:** New: the upstream connector, config file and grants, claim signer and JWKS route, a
`ToolSpec.input_schema` pass-through, startup registration of declared tools into the policy tables,
`email_verified` on the MCP principal, scoped-token domains (Settings form), an `upstreams` entry in
`privacyfence_status`, AI token proposals refusing upstream tools, and a reference upstream server.
Unchanged: desktop (local) mode refuses the file; no auto-accept rules or "Always allow" for upstream
tools yet; PrivacyFence never filters upstream results by domain; the knowledge hub's own claim
checks and row-level security belong to its repository.

**Security:** The claim carries identity across the boundary ADR 0036 kept closed: it is built
beside the transport, never by connector code, and names an agent only when attested. Grants match
only an email PrivacyFence treats as verified, which includes an allowed domain whose operator set
`trust_unverified_email`. Writes must be `popup`; a scoped token only narrows domains. A separate
RS256 key with an explicit `typ` keeps claims apart from OIDC ID tokens. An upstream's error text
reaches the AI client, cut to 500 characters.

**Performance:** Each upstream call opens one short MCP session; the tool list is fetched once at
startup.

**Review focus:** Startup mutation of the module-level policy tables (§3.4); the claim's actor and
service shapes against ADR 0006/0036 (§3.5); verified-email grants including trusted unverified
domains, and scoped-token narrowing (§3.3, §3.7); the verb rule that forbids an ungated write and
upstream error text reaching the client (§3.2, §3.6).

## 1. Goal

Issue [#1091](https://github.com/privacyfence/privacyfence/issues/1091). PrivacyFence becomes a
policy-enforcing proxy in front of any Streamable-HTTP MCP server ("upstream"). Tool-level policy
(which tools exist for a caller, which need approval) is enforced by PrivacyFence from the tool name
and arguments, like a built-in connector, with the usual cards, scoped tokens, unattended denial and
audit log. Data-level policy is not done in PrivacyFence: it resolves who the caller is and which data
domains they may see, and sends that as a signed claim on every upstream request, so the upstream
applies it inside its own queries (ranking and pagination stay correct, a guessed document id is
checked where the data lives). PrivacyFence stays the single place for identity, policy decisions,
approval and audit; the upstream trusts only PrivacyFence-signed claims and holds no credentials or
approvals.

Owner decisions (board ask `a-f3eba9a5d206e92e`, 2026-11-13): tools are **declared** by the operator;
domains come from **admin-config grants**, narrowed by scoped tokens; **organization servers only**;
the claim has `sub` = the person and an **actor** of `ai_client` or `workflow`.

## 2. Current state

- **Tools are static.** `ToolSpec`/`ToolParam` (`src/privacyfence/connector.py:16-75`) carry flat
  scalar params; `web/mcp_tools.py:73-100` builds every JSON schema from them. Connector-wide tests
  and `scripts/generate_tools_reference.py:128` build `cls(MagicMock())` and read `tool_specs()`;
  `connector_catalog.py:39` builds `cls(None)`. `tool_specs()` is synchronous and called on every
  request (`web/routes_mcp.py:449-454`, `:576-581`; `web/mcp_dispatch.py:240`, `:377`).
  `McpDispatcher.notify_tools_changed` (`web/mcp_dispatch.py:216`) announces a change.
- **Policy tables are module-level dicts keyed by tool name:** `auto_accept.TOOL_TO_GATE`
  (`auto_accept.py:218`), `auto_accept.TOOL_TO_OPERATION` (`:86`), `policy/registry.TOOL_TO_VERB`
  (`policy/registry.py:125`), `write_effects.EFFECT_BY_TOOL` (`write_effects.py:42`),
  `gate._TOOL_LAYOUT` (`gate.py:262`, values `NARROW`/`WIDE` from `approval_window_html`). Derived at
  import: `policy/registry.TOOL_REGISTRY` (`policy/registry.py:270`, from `_build_registry` at :258)
  and `policy/propose.ALL_OPERATIONS` (`policy/propose.py:91`, read as an attribute by
  `personal_settings.py:275`). Consumers that refuse an unknown tool: `mcp_dispatch.check_policy`
  (`web/mcp_dispatch.py:386`), `scoped_tokens.py:728`, `scoped_token_proposals.py:198`, the Settings
  tool list (`web/server.py:387`).
- **Gating lives in the connector**: `gate.gated_call` (`gate.py:940`) or an own audit row
  (`connectors/peek.py:889` `_auto_audit`). "Always allow" offers only scopes in
  `policy/propose.PROPOSABLE_SCOPES` (ADR 0077), so an operation with no scope gets no rule offer.
- **Tool-definition checks:** `tests/helpers.py:76-102` (`assert_tool_definitions_complete`) wants a
  `Returns` sentence and `Auto-approved` or `Requires user approval`; `test_tool_schema_portability.py`
  forbids `$ref`, `$defs`, `definitions`; `test_systemic_gate_invariants.py:214` wants a required
  `reason` `ToolParam` on every gated tool.
- **Errors:** `safe_errors.public_message` trusts only builtins and *named* `RuntimeError`
  subclasses (`safe_errors.py:93-125`); connectors re-raise client errors as named `RuntimeError`
  subclasses (`google_errors.GoogleResourceUnavailableError`, guidelines §1.4).
- **The caller** is three contextvars set around dispatch (`web/routes_mcp.py:503-504`):
  `current_principal()` (`principal.py:97`; `Principal(id, email, display_name, is_admin)`, `email`
  documented as cosmetic at `principal.py:49`), `current_agent()` (`agent_identity.py:259`;
  `is_attested()` true only for `OVERRIDE`/`OAUTH_CLIENT`, `:55`) and `current_token_scope()`
  (`scoped_tokens.py:974`). Connectors receive only `(tool, args)` (ADR 0036).
- **Verified email is not on the MCP principal.** `OrgOAuthProvider._mint_tokens`
  (`web/oauth_provider.py:802`) stores `email`, `display_name`, `is_admin` in the access-token claims
  and in `SealedRefreshRecord` (`web/sealed_refresh_store.py:119`); `principal_from_access_token`
  (`web/mcp_auth.py:280`) rebuilds the principal. `org_identity.session_email_verified`
  (`org_identity.py:490`) computes the bit; the browser session stores it
  (`web/org_session.py:54`). The provider's `self._policy` is an `AuthzPolicyConfig`
  (`web/oauth_provider.py:280`). `ScopedTokenVerifier` (`web/mcp_auth.py:225-245`) copies the owner's
  email from the token record.
- **Signing exists for OIDC only:** `oidc_provider.load_or_create_signing_key(path)` and `jwks(key)`
  (`oidc_provider.py:177`, `:198`; RS256, RSA 2048, mode 0600, ADR 0274), loaded only when
  `PRIVACYFENCE_OIDC_CLIENTS` is set (`daemon_main.py:1503-1510`); `/oidc/jwks` is mounted at
  `web/server.py:1383`; the org app's lifespans list is built in `server._build_org_app`
  (`web/server.py:1361-1372`).
- **No outbound MCP client in `src/`.** `sync/src/privacyfence_sync/client.py:50-70` shows the
  pattern. `mcp` is locked at 2.3.0 (Python attributes `Tool.input_schema`,
  `CallToolResult.structured_content`, `CallToolResult.is_error`); `httpx2` is a dev dependency
  (`pyproject.toml:250-260`) and already in `requirements/runtime.lock.txt:510` via `mcp`.
- **Mode guard pattern:** `daemon_main.py:974-978` refuses `PRIVACYFENCE_OIDC_CLIENTS` outside
  organization mode. `org_mode.connector_enabled_by_org` (`org_mode.py:949`) raises `KeyError` for a
  name not in `CONNECTOR_ORG_SECTION`.

## 3. Design

### 3.1 Names

| Thing | Name |
|---|---|
| Config file variable | `PRIVACYFENCE_UPSTREAM_MCP_FILE` |
| Config module | `src/privacyfence/upstream_mcp_config.py` |
| Claim module | `src/privacyfence/upstream_claims.py` |
| Table registration | `src/privacyfence/upstream_mcp_registry.py` |
| MCP client and catalog | `src/privacyfence/upstream_mcp_client.py` (`UpstreamMcpClient`, `CallResult`, `UpstreamMcpClientError`, `UpstreamToolCatalog`) |
| Connector | `src/privacyfence/connectors/upstream_mcp.py` (`UpstreamMcpConnector`, `UpstreamUnavailableError`, `UpstreamToolError`) |
| Startup wiring | `src/privacyfence/upstream_mcp_runtime.py` |
| JWKS route | `src/privacyfence/web/routes_upstream_mcp.py`, path `/upstream-mcp/jwks` |
| Signing key file | `org_dir() / "upstream_claim_signing_key.pem"` |
| Reference upstream | `scripts/upstream_mcp_reference_server.py` |
| Operator and implementer doc | `docs/upstream-mcp.md` |
| Catalog name for tools reference | `"upstream_mcp"`, title `Upstream MCP servers` |

### 3.2 The operator file

JSON, read once at startup. Any error raises `org_mode.ConfigurationError` and refuses the start,
naming the JSON path of the bad value (never the value).

```json
{
  "version": 1,
  "upstreams": [
    {
      "id": "kh",
      "label": "Knowledge hub",
      "url": "https://kh.example.internal/mcp",
      "audience": "https://kh.example.internal",
      "tools": {
        "search":         {"gate": "auto",  "verb": "search"},
        "get_doc":        {"gate": "auto",  "verb": "read"},
        "propose_change": {"gate": "popup", "verb": "add",    "effect": "Adds a change proposal to the knowledge hub."},
        "write_derived":  {"gate": "popup", "verb": "update", "effect": "Writes a derived document to the knowledge hub."}
      }
    }
  ]
}
```

Rules (each message starts `PRIVACYFENCE_UPSTREAM_MCP_FILE: `):

- `version` must be `1`. Top-level keys other than `version`, `upstreams` are refused, as are
  unknown keys in an upstream or a tool. At most 8 upstreams, at most 50 tools per upstream.
- `id` matches `^[a-z][a-z0-9]{1,15}$`; ids are unique. With `BUILTIN_NAMES` = the keys of
  `org_mode.CONNECTOR_ORG_SECTION` plus `"privacyfence"` and `"upstream_mcp"`, an id is refused when
  `(id + "_").startswith(name + "_")` or `(name + "_").startswith(id + "_")` for any name. Message:
  `upstream id "<id>" collides with the built-in connector "<name>"`.
- `label`: 1-60 characters, no control characters.
- `url`: `https://` any host, or `http://` only when the host is `localhost`, a loopback IP literal,
  or a single-label name with no dot (a container on the suite network, e.g.
  `http://knowledge-hub:8080/mcp`). No userinfo, no fragment.
- `audience`: required, 1-200 characters, no whitespace.
- Tool names match `^[A-Za-z0-9_-]{1,48}$`; the exposed name `<id>_<tool>` is at most 64 characters.
- `gate` is one of `auto`, `review`, `popup`; `verb` is a `policy.registry.Verb` value.
- **Read verbs** (`read`, `download`, `search`) take `gate` `auto` or `review` and no `effect`;
  **every other verb** takes `gate` `popup` and must carry `effect` (1-200 characters, ending in
  `.`). So an ungated write cannot be declared. (Declaring a writing tool with a read verb is the
  operator's error; the doc warns about it.)
- Derived for `ToolSpec`: `read_only = verb in READ_VERBS`, `destructive = verb == "delete"`.

```python
@dataclass(frozen=True)
class DeclaredTool:
    name: str            # upstream tool name, e.g. "search"
    exposed_name: str    # "<id>_<name>", e.g. "kh_search"
    operation: str       # "<id>.<name>", e.g. "kh.search"
    gate: str            # "auto" | "review" | "popup"
    verb: str            # a Verb value
    effect: str          # "" unless popup

@dataclass(frozen=True)
class UpstreamServer:
    id: str
    label: str
    url: str
    audience: str
    tools: tuple[DeclaredTool, ...]   # in file order

def load_upstreams(path: Path) -> tuple[UpstreamServer, ...]: ...
def parse_upstreams(data: object, *, source: str) -> tuple[UpstreamServer, ...]: ...
```

### 3.3 Domain grants

In `admin_config.json`, top-level key `upstream_mcp` (default `{}`):

```json
"upstream_mcp": {
  "kh": {"grants": [
    {"match": "*",               "domains": ["general"]},
    {"match": "@hr.example.com", "domains": ["hr"]},
    {"match": "ceo@example.com", "domains": ["hr", "finance"]}
  ]}
}
```

- `match` is `*` (any verified person), `@<domain>` (exact, case-insensitive email domain) or an
  exact case-insensitive email. Domain names match `^[a-z0-9][a-z0-9_-]{0,62}$`. At most 200 grants
  per upstream. A grant for an undeclared upstream id is logged once as a warning and ignored. An
  invalid section is reported as `admin_config.json: upstream_mcp.<id>.grants[<n>]: <problem>`.
- "Verified" means `Principal.email_verified` (§3.7, p3), which is
  `org_identity.session_email_verified`: the identity provider said so, **or** the operator set
  `trust_unverified_email` and the address is in an allowed domain. Single-user mode is verified
  (ADR 0272).
- `domains_for(grants, upstream_id, email, email_verified) -> tuple[str, ...]`: `()` when
  `email_verified` is false or `email` is empty; otherwise the sorted union of every matching grant.
- `GrantStore(path)` re-reads the file when its `st_mtime_ns` changes (one `stat` per call), so an
  edit applies to the next call without a restart. The first load raises `ConfigurationError` on an
  invalid section (start refused); a later re-read that fails keeps the last good grants and logs an
  error. A missing file or key means no grants.
- No Admin page control in this release; it is a file edit (documented).

### 3.4 Registering declared tools

`upstream_mcp_registry.register(upstreams)` runs once at startup, before the web server serves, and
writes for every `DeclaredTool`:

- `auto_accept.TOOL_TO_GATE[exposed] = gate`
- `auto_accept.TOOL_TO_OPERATION[exposed] = operation`
- `policy.registry.TOOL_TO_VERB[exposed] = Verb(verb)`
- `write_effects.EFFECT_BY_TOOL[exposed] = effect` (popup only)
- `gate._TOOL_LAYOUT[exposed] = approval_window_html.WIDE` (review and popup only)

then `policy.registry.rebuild_registry()` (new: `TOOL_REGISTRY.clear()` then
`TOOL_REGISTRY.update(_build_registry())`, in place) and `policy.propose.refresh_all_operations()`
(new: rebinds the module global `ALL_OPERATIONS` from `TOOL_REGISTRY`). An exposed name already in
`TOOL_TO_GATE` raises `ConfigurationError('PRIVACYFENCE_UPSTREAM_MCP_FILE: tool "<exposed>" collides
with an existing tool')` before anything is written. `unregister_all()` removes every name `register`
added (a module-level set) and rebuilds both derived values. No scope selector, catalogue entry or
proposable scope is added, so upstream tools get no "Always allow" offer and no rule; rules for them
are a later change. Rejected: lookup functions replacing direct dict access in ~15 modules (large
diff for tools that change only at restart).

### 3.5 The claim

A JWT, RS256, signed with its own key (`load_or_create_signing_key(org_dir() /
"upstream_claim_signing_key.pem")`), published at `/upstream-mcp/jwks` (`oidc_provider.jwks(key)`).
Header: `{"alg": "RS256", "kid": <key.kid>, "typ": "pf-upstream-claim+jwt"}`. Payload:

| Claim | Value |
|---|---|
| `iss` | `<issuer_url without trailing slash>/upstream-mcp` |
| `aud` | the upstream's `audience` |
| `sub` | `principal.id`; `"privacyfence"` for a service claim |
| `email` | `principal.email` when verified, else `""` |
| `email_verified` | the verified bit |
| `domains` | sorted list (§3.3, §3.7) |
| `act` | below |
| `pf_upstream` | upstream id |
| `pf_tool` | the upstream tool name for `tools/call`, `null` otherwise |
| `iat`, `nbf` | now, integer seconds |
| `exp` | `iat + 60` |
| `jti` | `uuid.uuid4().hex` |

`act`:
- a scoped token in scope (`current_token_scope()` not `None`, checked first):
  `{"type": "workflow", "token_id": scope.id, "label": scope.label}`.
- otherwise `{"type": "ai_client", "attested": <bool>, "name": <agent.name if attested else "">,
  "claimed_name": <agent.name if not attested else "">}`; no agent gives attested false and both
  names `""`.
- service claim (the startup `tools/list`): `{"type": "service"}`, `sub` `"privacyfence"`, `email`
  `""`, `email_verified` false, `domains` `[]`.

The service actor is a third value beyond the owner's two; it carries no person and no domains, so
an upstream following the contract returns no data for it. `docs/upstream-mcp.md` tells
implementers that only `sub`, `domains` and `act.type` may change what a query returns; `name`,
`claimed_name` and `label` are for their audit only (ADR 0006 decision 3).

```python
CLAIM_TYPE = "pf-upstream-claim+jwt"
CLAIM_LIFETIME_SECONDS = 60
KEY_FILENAME = "upstream_claim_signing_key.pem"

@dataclass(frozen=True)
class CallerClaim:
    sub: str
    email: str
    email_verified: bool
    domains: tuple[str, ...]
    act: dict[str, object]

class ClaimSigner:
    def __init__(self, *, issuer: str, key: oidc_provider.SigningKey) -> None: ...
    def jwks(self) -> dict[str, object]: ...
    def mint(self, *, upstream: UpstreamServer, caller: CallerClaim, tool: str | None,
             now: float | None = None) -> str: ...

def load_signer(issuer_url: str) -> ClaimSigner: ...
def actor_from_context() -> dict[str, object]: ...   # current_token_scope(), current_agent()
def service_caller() -> CallerClaim: ...

class ClaimAuth(httpx2.Auth):   # a fresh JWT (own jti) per HTTP request: "Authorization: Bearer <jwt>"
    def __init__(self, signer: ClaimSigner, upstream: UpstreamServer, caller: CallerClaim,
                 tool: str | None) -> None: ...
```

Connector code never reads the contextvars: the connector calls a `caller` callable that
`upstream_mcp_runtime.caller_for` supplies (§3.8). Rejected: reusing the OIDC key (ties the feature to
`PRIVACYFENCE_OIDC_CLIENTS`, lets a claim be confused with a Peek/Grist ID token on one JWKS, couples
rotation); a custom header (MCP servers already verify bearer tokens); longer lifetime or claim
caching (a revoked grant must stop within a minute).

### 3.6 Client, catalog and connector

- `UpstreamMcpClient(upstream, signer, *, transport: httpx2.AsyncBaseTransport | None = None)`:
  `async list_tools() -> list[mcp.types.Tool]` (service claim) and `async call_tool(tool, arguments,
  caller) -> CallResult`. Each call opens its own `httpx2.AsyncClient(auth=ClaimAuth(...),
  timeout=httpx2.Timeout(60.0, connect=10.0), transport=transport)`,
  `streamable_http_client(upstream.url, http_client=...)`, `ClientSession.initialize()`, one request,
  close. Never shared across calls or principals.
- `UpstreamMcpClientError(Exception)` with `kind` one of `"unreachable"`, `"too_large"`,
  `"tool_error"` and `detail` (the upstream error text for `tool_error`, else `""`). Raised for
  transport failures, text over 1,000,000 characters, and a result with `is_error`.
- `CallResult(text: str, structured: dict | None, dropped_blocks: int)`: every `TextContent` text
  joined with `"\n"`, plus `structured_content` when it is a dict; other blocks are counted.
- `UpstreamToolCatalog(upstream)`: `update(tools: list[mcp.types.Tool])` keeps the last snapshot;
  `specs() -> list[ToolSpec]` gives the declared tools the snapshot contains, in file order;
  `missing() -> list[str]` the declared exposed names it lacks or skipped. Each spec:
  - `name` = exposed name.
  - `description` = upstream description with control characters removed and cut to 700
    characters, then ` Returns what the <label> server answers.`, then by gate: `auto`
    ` Auto-approved: the <label> server decides which data the caller may see.`; `review`
    ` Requires user approval: PrivacyFence shows the result to the person before it is returned.`;
    `popup` ` Requires user approval: PrivacyFence asks the person before the call is sent.`
  - `params` = `[ToolParam("reason", "str", required=True, description="One sentence: why are you
    calling this tool right now?")]` (so the systemic `reason` invariant holds), and
    `input_schema` = a deep copy of the upstream `Tool.input_schema` with the same `reason` property
    added to `properties` and to `required`.
  - A tool is skipped (WARNING log naming the exposed name and the reason) when its schema is not a
    dict with `"type": "object"`, already has a `reason` property, serializes to more than 32 KB, or
    contains a `$ref`, `$defs` or `definitions` key at any depth.
  - `read_only`, `destructive` from §3.2.
- `UpstreamMcpConnector(upstream=None, catalog=None, client=None, caller=None)`: `name` is
  `upstream.id` when `upstream` is an `UpstreamServer`, else `"upstream_mcp"`; `tool_specs()` returns
  `catalog.specs()` when `catalog` is an `UpstreamToolCatalog`, else `[]` (so `cls(None)` and
  `cls(MagicMock())` give no tools). `call(tool, args)`: an unknown name raises
  `ValueError(f"Unknown upstream tool: {tool!r}")`.
  - `auto`: call upstream, write an `auto_accepted` audit row (`_auto_audit` pattern,
    `auto_accept_rule="auto"`), return the result.
  - `review`: call upstream, then `return await gated_call(..., gate="review", raw_data=value,
    filtered_data=value, pii_scan_text=text, details_text=text, args=args)`.
  - `popup`: `await gated_call(..., gate="popup", raw_data=args, filtered_data=None,
    details_text=<json.dumps(args, indent=2, sort_keys=True)>, write_content_scan_text=<same>,
    args=args)` first; then call upstream and return the result.
  - Every gated call: `tool_name=f"{upstream.label}: {tool}"`, `summary=f"{verb.capitalize()} on
    {upstream.label}"`, `sender=upstream.label`, `preview={"Server": upstream.label, "Tool": tool,
    "Arguments": ", ".join(sorted(args)) or "none"}` (names only, never values).
  - Returned value: `CallResult.structured` when not `None`, else `CallResult.text`.
  - Errors: `UpstreamMcpClientError` is caught and re-raised as `UpstreamUnavailableError(RuntimeError)`
    with `f"The {upstream.label} server could not be reached."` (`unreachable`) or `f"The
    {upstream.label} result is larger than 1 MB; ask for less."` (`too_large`), or as
    `UpstreamToolError(RuntimeError)` with `f"The {upstream.label} server returned an error:
    {detail}"`, `detail` with control characters removed and cut to 500 characters. Both are named
    `RuntimeError` subclasses, so `safe_errors.public_message` passes them (the
    `google_errors.GoogleResourceUnavailableError` pattern); `safe_errors.py` is not changed. The
    upstream's error text is its own answer to the caller, like its results (ADR 0278).

### 3.7 Verified email and scoped-token domains

- `Principal.email_verified: bool = False`, set at IdP sign-in from
  `org_identity.session_email_verified(claims, principal, self._policy)`, carried in access-token
  claims and `SealedRefreshRecord` (missing key decodes as `False`), read by
  `principal_from_access_token` only as the literal `True`. `SINGLE_USER_PRINCIPAL` has it `True`.
- `ScopedToken` gains `upstream_domains: Mapping[str, frozenset[str]]` (default empty) and
  `principal_email_verified: bool` (default `False`), serialized only when non-default (the
  `allow_personal_data_reads` pattern, `scoped_tokens.py:219`, `:246`, `:310`), no file-version bump.
  `ScopedTokenVerifier` adds `"email_verified": found.principal_email_verified` to the claims.
- `ScopedTokenStore.create` (`scoped_tokens.py:595`) gains keyword arguments
  `upstream_domains: Mapping[str, Iterable[str]] | None = None`, `held_domains: Mapping[str,
  frozenset[str]] | None = None` and `principal_email_verified: bool = False`; a domain not in
  `held_domains[id]` raises `ScopedTokenError("You can only give a token domains you have: <domain>")`
  (the store's existing error type). `normalise_create_request` (`scoped_tokens.py:205`) normalises
  `upstream_domains` to `{id: sorted list}` so the step-up fingerprint covers it.
  `scoped_tokens.py` never imports `upstream_mcp_runtime`; the Settings route passes
  `held_domains` from `upstream_mcp_runtime.held_domains(principal, email_verified)`.
- For a scoped call: domains = `domains_for(id, owner email, token.principal_email_verified) ∩
  token.upstream_domains.get(id, ∅)`. A token never widens; a missing entry gives none; tokens made
  before this change send `domains: []`.
- Settings "New token" form: for each upstream with a chosen tool, a fieldset `Data domains for
  <label>` with one checkbox per domain the person holds, none checked, hint `The token sees only
  the domains you tick, and never more than you can see yourself.`
- AI token proposals (`scoped_token_proposals.py`, ADR 0250) refuse upstream tools:
  `Upstream tools cannot be proposed; create the token in Settings.`

### 3.8 Startup wiring (organization mode)

```python
def configure(*, upstreams, signer, grants: GrantStore, transport=None) -> None: ...
def configured() -> bool: ...
def reset() -> None: ...                                         # tests
def upstream_ids() -> frozenset[str]: ...
def connectors_for_principal() -> list[UpstreamMcpConnector]: ...
def caller_for(upstream: UpstreamServer) -> CallerClaim: ...
def held_domains(principal: Principal, email_verified: bool) -> dict[str, frozenset[str]]: ...
def status() -> list[dict[str, object]]: ...
def lifespan(notify_tools_changed: Callable[[], None]): ...      # an async context manager factory
```

- `daemon_main`: outside organization mode, a non-empty `PRIVACYFENCE_UPSTREAM_MCP_FILE` raises
  `ConfigurationError("PRIVACYFENCE_UPSTREAM_MCP_FILE is set, but this is not an organization
  server.")` beside `daemon_main.py:974`. In organization mode with it set, before the web server is
  built: `load_upstreams`, `GrantStore(admin_config.admin_config_path())` (first load validates),
  `register()`, `load_signer(server_config.issuer_url)`, `configure()`; `build_connectors` appends
  `connectors_for_principal()` when `configured()`.
- `web/server._build_org_app` appends `upstream_mcp_runtime.lifespan(mcp_dispatcher.notify_tools_changed)`
  to its lifespans and mounts `routes_upstream_mcp.build_routes(...)` beside `routes_oidc` (`:1383`),
  both only when `configured()`.
- `lifespan`: one task per upstream; `list_tools()` with backoff 5 s doubling to 300 s until the first
  success, then `catalog.update(...)` and `notify_tools_changed()` once. No periodic refresh (a
  description changing mid-session is a rug-pull vector).
- Every lookup keyed by connector name over the live connector set (`CONNECTOR_ORG_SECTION[`,
  `settings_controller.ALL_CONNECTORS`, `_CONNECTOR_LABEL_OVERRIDES`, `connector_enabled_by_org`)
  skips names in `upstream_ids()`.
- `privacyfence_status` gains `upstreams`: `[{"id", "label", "tools_exposed", "state": "ready" |
  "waiting_for_tool_list", "missing_tools": [...]}]`, only when `configured()`.

### 3.9 Testing approach

No live provider: no OAuth sign-in and no third-party API to drift, so the two-PR connect/tools
procedure and the layer-5 live fixture do not apply; `scripts/qa_fixture_recorder.py` does not list
this connector. `scripts/upstream_mcp_reference_server.py` is a small upstream that verifies claims
(JWKS URL, JWKS file or PEM, `typ`, `aud`, `iss`, `exp`, a `jti` replay cache) and serves `search`,
`get_doc`, `propose_change` over in-memory documents with a `domain` field, filtering by the claim's
`domains`. Unit tests drive it in process; the integration test runs it on loopback against a real
organization server.

## 4. ADRs

- **0278** — Upstream MCP servers are declared connectors: the operator file, `<id>_<tool>` naming,
  gates fixed by verb (writes are always `popup`), startup registration into the policy tables, no
  rules for upstream tools yet, organization servers only, the upstream's error text reaches the
  client (cut to 500 characters), and no live fixture (layer 5 does not apply). Relates to 0077,
  0115, 0196.
- **0279** — PrivacyFence sends the upstream a signed caller claim: separate RS256 key and
  `/upstream-mcp/jwks`, `typ` `pf-upstream-claim+jwt`, the claim set, 60-second lifetime, one claim
  per HTTP request, the `act` shapes including `service`, built beside the transport. Amends 0036;
  relates to 0006, 0273, 0274.
- **0280** — Data domains come from admin-config grants on a verified email (including
  `trust_unverified_email` domains), `email_verified` is carried on the MCP principal, and a scoped
  token can only narrow domains. Amends 0164 and 0236; relates to 0272.

## 5. Manual steps

**Before implementation:** none.

**After implementation**

1. **ma1-reference-upstream-real-client** (about 30 minutes, on a computer with Docker and Claude
   Code). Checks what CI cannot: a real AI client accepts the passed-through schemas, grant edits
   apply live, and an upstream write shows an approval card. Setup docs:
   [Run it on this computer](https://github.com/privacyfence/privacyfence/blob/feature/upstream-mcp-connector/docs/org-mode-docker.md#run-it-on-this-computer),
   [Connect Claude Code, Single-user Docker](https://github.com/privacyfence/privacyfence/blob/feature/upstream-mcp-connector/docs/connect-claude-code.md#single-user-docker),
   [docs/upstream-mcp.md](https://github.com/privacyfence/privacyfence/blob/feature/upstream-mcp-connector/docs/upstream-mcp.md).
   1. In a checkout of `feature/upstream-mcp-connector`, run
      `bash scripts/build_org_image.sh --tag privacyfence-org:upstream-test`. It ends with the
      image tag printed (about 10 minutes).
   2. Run `mkdir -p ~/pf-upstream-test && docker network create pf-upstream-test`.
   3. Save the "Reference upstream" example from `docs/upstream-mcp.md` as
      `~/pf-upstream-test/upstream_mcp.json` (`id` `ref`, `url` `http://refup:8099/mcp`, `audience`
      `ref-upstream`).
   4. Start PrivacyFence: `docker run --rm -d --name privacyfence --network pf-upstream-test
      -p 127.0.0.1:8765:8765 -v pf-upstream-data:/var/lib/privacyfence-org/.privacyfence
      -v ~/pf-upstream-test:/config:ro -e PRIVACYFENCE_UPSTREAM_MCP_FILE=/config/upstream_mcp.json
      privacyfence-org:upstream-test`. Sign in with the setup code from `docker logs privacyfence`
      and add a passkey, as the first linked doc says.
   5. Run `curl -s http://localhost:8765/upstream-mcp/jwks > ~/pf-upstream-test/jwks.json`. The file
      holds one RSA key.
   6. Start the reference upstream: `docker run --rm -d --name refup --network pf-upstream-test
      -v ~/pf-upstream-test:/config:ro -v "$PWD/scripts":/scripts:ro --entrypoint python3
      privacyfence-org:upstream-test /scripts/upstream_mcp_reference_server.py --host 0.0.0.0
      --port 8099 --audience ref-upstream --issuer http://localhost:8765/upstream-mcp
      --jwks-file /config/jwks.json`. Within five minutes `docker logs privacyfence` shows the
      `ref` upstream as ready.
   7. Give yourself every domain: run the `docker exec` command from the "Reference upstream"
      section of `docs/upstream-mcp.md` that sets the grants `*` → `general` and
      `admin@privacyfence.localhost` → `general, hr, finance`.
   8. Connect Claude Code as the second linked doc says and ask it to call `ref_search` for
      `policy`. You see results from the `general`, `hr` and `finance` domains and no approval card.
   9. Run the same doc's command that keeps only the `*` grant, then search again: only `general`
      results.
   10. Ask Claude Code to call `ref_propose_change` on any document. An approval card
       `Reference upstream: propose_change` appears in PrivacyFence; approve it with your passkey;
       Claude Code reports the proposal accepted.
   11. Clean up: `docker rm -f privacyfence refup && docker network rm pf-upstream-test &&
       docker volume rm pf-upstream-data`.
   Report back: `ma1: pass` or `ma1: fail at step <n>: <what you saw>`.

## 6. Risks and open questions

- **Mutating module-level tables** (§3.4): a consumer that cached a derived copy at import would miss
  upstream tools. p5 stops with `status=blocked` naming any consumer other than `TOOL_REGISTRY` and
  `ALL_OPERATIONS` that does.
- **Static checks do not see dynamic tools:** the connector-wide tests build `cls(MagicMock())` (no
  tools); p8 runs `assert_tool_definitions_complete` and the portability checks on a connector built
  from a fake catalog.
- **Schema portability (ADR 0115):** upstream schemas may use `enum` or nested types some clients
  reject; ma1 checks one real client; the doc makes it the operator's responsibility.
- **The upstream must verify `typ`, `aud`, `iss`, `exp` and `jti`.** PrivacyFence cannot enforce it;
  the doc and the reference server are the contract.
- **Review gate order:** a `review` read queries the upstream before approval, like every connector;
  a writing tool declared with a read verb bypasses the popup. The doc warns; the verb rule removes
  only the accidental case.
- **Old refresh chains carry no `email_verified`** and resolve as unverified (no domains) until the
  person signs in again: fail-closed by design.
- **A connector name that is not a built-in** (the upstream id) could reach a lookup that raises
  `KeyError`; p8 makes each one skip upstream ids, and its integration test calls
  `privacyfence_status`. A phase that meets another such lookup stops with `status=blocked`.
- **CONTRIBUTING.md says plans are deleted when work lands**; the tld profile keeps
  `docs/<n>/plan.md` as a historical record. This plan follows the profile; code comments link ADRs,
  never the plan.
- **Audit correlation** with the upstream (recording `jti` in the chained audit row, ADR 0071) is left
  out; a later change.

## Implementation manifest

```yaml
plan_slug: upstream-mcp-connector
feature_branch: feature/upstream-mcp-connector
tracking_issue: 1091
max_parallel: 3
manual_before: []
manual_after:
  - id: ma1-reference-upstream-real-client
    title: Run the feature-branch image and the reference upstream in Docker, and call them from Claude Code with two grant sets
    why: CI cannot show that a real AI client accepts passed-through schemas, that grant edits apply live, and that an approval card appears for an upstream write
verify_after_merge:
  - python3 -m pytest tests/unit -q -k "upstream or scoped_token or mcp_tools or principal"
  - python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q
final_checks:
  - "`tld plan deviations check --file docs/1091/deviations.md --plan docs/1091/plan.md --all` exits 0"
  - ADRs 0278, 0279 and 0280 exist in docs/adr/, each with Status Accepted, and each has a row in docs/adr/README.md
  - CHANGELOG.md has the upstream MCP entries under "## [Unreleased]" and no new version heading
phases:
  - id: p1-tool-input-schema
    title: Let a ToolSpec carry a raw JSON input schema
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/connector.py
      - src/privacyfence/web/mcp_tools.py
      - tests/unit/test_connector.py
      - tests/unit/web/test_mcp_tools.py
    brief: |
      Read first: docs/1091/plan.md §3.6; docs/coding-and-testing-guidelines.md §1.5
      1. src/privacyfence/connector.py: add to ToolSpec, after `destructive`, the field
         `input_schema: dict[str, Any] | None = None` with the comment "A complete JSON Schema
         used verbatim instead of one built from params (upstream MCP tools)."
      2. ToolSpec.to_dict adds `"input_schema": self.input_schema`; from_dict passes
         `input_schema=d.get("input_schema")`.
      3. src/privacyfence/web/mcp_tools.py: tool_input_schema(spec) returns
         `copy.deepcopy(spec.input_schema)` when it is not None, before building from params.
      4. Tests: tests/unit/test_connector.py `test_to_dict_round_trips_input_schema` (with and
         without); tests/unit/web/test_mcp_tools.py `test_tool_input_schema_uses_raw_schema_verbatim`
         (a nested object and an array survive) and `test_tool_input_schema_returns_a_copy`.
      5. Stop with status=blocked if code outside mcp_tools.py builds the MCP schema from
         spec.params (grep `spec.params` and `.params` in src/privacyfence/web).
    acceptance:
      - python3 -m pytest tests/unit/test_connector.py tests/unit/web/test_mcp_tools.py -q passes
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q passes
      - grep -n "input_schema: dict\[str, Any\] | None = None" src/privacyfence/connector.py matches
  - id: p2-upstream-config
    title: Parse the operator file and the admin-config domain grants
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_config.py
      - src/privacyfence/admin_config.py
      - tests/unit/test_upstream_mcp_config.py
      - tests/unit/test_admin_config.py
    brief: |
      Read first: docs/1091/plan.md §3.1, §3.2, §3.3; src/privacyfence/oidc_provider.py:56-136; src/privacyfence/admin_config.py:116-215
      1. Create src/privacyfence/upstream_mcp_config.py: a module docstring summarising §3.2-§3.3
         and naming ADR 0278 and ADR 0280; `ENV_VAR = "PRIVACYFENCE_UPSTREAM_MCP_FILE"`,
         `READ_VERBS = frozenset({"read", "download", "search"})`, `BUILTIN_NAMES` per §3.2;
         DeclaredTool, UpstreamServer, load_upstreams, parse_upstreams exactly as §3.2. Implement
         every §3.2 rule in order; each error is org_mode.ConfigurationError starting
         `PRIVACYFENCE_UPSTREAM_MCP_FILE: ` and naming the JSON path (for example
         `upstreams[0].tools.search.gate`), never the value. Verbs come from policy.registry.Verb.
      2. Same module: `@dataclass(frozen=True) class Grant: match: str; domains: tuple[str, ...]`,
         `parse_grants(section: object, upstream_ids: frozenset[str]) -> dict[str, tuple[Grant, ...]]`,
         the pure `domains_for(grants, upstream_id, email, email_verified) -> tuple[str, ...]`, and
         `class GrantStore` with `__init__(self, path: Path, upstream_ids: frozenset[str])` and
         `domains_for(upstream_id, email, email_verified)`, per §3.3 (first load raises
         ConfigurationError; later bad re-read keeps the last good grants and logs; mtime_ns
         re-read; unknown ids warned once and ignored; missing file or key gives no grants).
      3. src/privacyfence/admin_config.py: add `"upstream_mcp": {}` to _default_config's result
         (_fill keeps unknown nested keys, admin_config.py:152-164).
      4. Tests in tests/unit/test_upstream_mcp_config.py: a valid file; one refusal test per §3.2
         rule, each asserting the path is in the message and the value is not; the http host rule
         (localhost, 127.0.0.1, `knowledge-hub` allowed; `kh.example.com` refused); the verb/gate
         and effect rules; the id collision rule (`slack` and `privacyfence` refused, `slac` and
         `slackx` allowed, `kh` allowed); grants `*`, `@domain`, exact email
         (case-insensitive), sorted union, unverified gives (), empty email gives (); GrantStore
         re-read on an mtime change (os.utime with a new ns value), a bad re-read keeps the last
         good grants, a bad first load raises. tests/unit/test_admin_config.py: the default config
         has `upstream_mcp == {}`.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_config.py tests/unit/test_admin_config.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_config.py -q --cov=privacyfence.upstream_mcp_config --cov-branch --cov-fail-under=100 passes
      - ruff check src/privacyfence/upstream_mcp_config.py passes
  - id: p3-verified-email
    title: Carry email_verified on the MCP principal in organization mode
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/principal.py
      - src/privacyfence/web/oauth_provider.py
      - src/privacyfence/web/sealed_refresh_store.py
      - src/privacyfence/web/mcp_auth.py
      - src/privacyfence/web/single_user_auth.py
      - tests/unit/test_principal.py
      - tests/unit/web/test_oauth_provider.py
      - tests/unit/web/test_mcp_auth.py
      - tests/unit/web/test_sealed_refresh_store.py
    brief: |
      Read first: docs/1091/plan.md §3.3, §3.7 (first bullet); docs/adr/0272-a-single-user-install-provisions-its-one-person-as-verified.md; docs/adr/0072-org-mode-persists-only-sealed-refresh-tokens.md
      1. principal.py: add `email_verified: bool = False` to Principal after is_admin; extend the
         docstring: "email_verified is whether PrivacyFence treats the email as verified
         (org_identity.session_email_verified, set at sign-in); upstream domain grants key on email
         only when it is true (ADR 0280)."
      2. web/single_user_auth.py: SINGLE_USER_PRINCIPAL gets `email_verified=True`.
      3. web/oauth_provider.py handle_idp_callback: after check_authz_policy,
         `principal = dataclasses.replace(principal, email_verified=org_identity.session_email_verified(claims, principal, self._policy))`.
      4. _mint_tokens adds `"email_verified": principal.email_verified` to the access-token claims;
         _OrgRefreshToken and SealedRefreshRecord gain `email_verified: bool = False`, set at mint
         and carried on rotation; the sealed-record decoder treats a missing key as False; wherever
         a Principal is rebuilt from a refresh record, pass email_verified through.
      5. web/mcp_auth.py principal_from_access_token: `email_verified=claims.get("email_verified") is True`.
         ScopedTokenVerifier is unchanged here (p9).
      6. Tests: Principal default False; SINGLE_USER_PRINCIPAL True; an IdP callback with
         email_verified true yields a minted token whose principal is verified, false yields not;
         refresh rotation keeps it; a sealed record without the key decodes False;
         principal_from_access_token gives False for the string "true".
    acceptance:
      - python3 -m pytest tests/unit/test_principal.py tests/unit/web/test_oauth_provider.py tests/unit/web/test_mcp_auth.py tests/unit/web/test_sealed_refresh_store.py -q passes
      - python3 -m pytest tests/integration/test_shim_mcp_contract.py -q passes
      - grep -n "email_verified: bool = False" src/privacyfence/principal.py matches
  - id: p4-upstream-claims
    title: Mint and publish the signed caller claim
    depends_on: [p2-upstream-config]
    complexity: M
    touches:
      - src/privacyfence/upstream_claims.py
      - tests/unit/test_upstream_claims.py
    brief: |
      Read first: docs/1091/plan.md §3.5; src/privacyfence/oidc_provider.py:138-220 and :348-390; docs/adr/0006-attributing-a-request-to-the-ai-system-that-made-it.md; docs/adr/0036-card-copy-names-the-caller-through-one-placeholder.md
      1. Create src/privacyfence/upstream_claims.py with everything in §3.5's code block. mint uses
         `jwt.encode(payload, key.private_key, algorithm="RS256", headers={"kid": key.kid, "typ": CLAIM_TYPE})`;
         jwks() returns oidc_provider.jwks(key); load_signer calls
         oidc_provider.load_or_create_signing_key(paths.org_dir() / KEY_FILENAME) and sets issuer
         to `issuer_url.rstrip("/") + "/upstream-mcp"`. The module docstring names ADR 0279.
      2. actor_from_context per §3.5: scoped_tokens.current_token_scope() first, then
         agent_identity.current_agent() and its is_attested().
      3. ClaimAuth subclasses httpx2.Auth; auth_flow sets
         `request.headers["Authorization"] = "Bearer " + signer.mint(...)` (a fresh mint per
         request) and yields the request.
      4. Tests in tests/unit/test_upstream_claims.py with a tmp_path key: decode with jwt.decode
         against the JWKS public key, audience and issuer; header typ and kid; every §3.5 claim;
         exp == iat + 60; two mints differ in jti; email "" when unverified; actor for a scoped
         token, an attested agent, a claimed CLIENT_INFO agent, no agent; service_caller; ClaimAuth
         puts different bearers on two requests (drive auth_flow with two httpx2.Request objects);
         a wrong audience raises jwt.InvalidAudienceError.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_claims.py -q passes
      - python3 -m pytest tests/unit/test_upstream_claims.py -q --cov=privacyfence.upstream_claims --cov-branch --cov-fail-under=100 passes
  - id: p5-tool-registration
    title: Register declared upstream tools into the policy tables at startup
    depends_on: [p2-upstream-config]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_registry.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/policy/propose.py
      - tests/unit/test_upstream_mcp_registry.py
      - tests/unit/policy/test_registry.py
      - tests/unit/policy/test_propose.py
      - tests/conftest.py
    brief: |
      Read first: docs/1091/plan.md §3.4; docs/coding-and-testing-guidelines.md §3 "Gate and policy tables"; docs/adr/0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md
      1. policy/registry.py: `def rebuild_registry() -> None:` `TOOL_REGISTRY.clear()` then
         `TOOL_REGISTRY.update(_build_registry())`.
      2. policy/propose.py: `def refresh_all_operations() -> None:` with `global ALL_OPERATIONS`,
         rebinding it with the same expression as its definition at :91.
      3. Create src/privacyfence/upstream_mcp_registry.py with register(upstreams) and
         unregister_all() exactly per §3.4 (collision check before any write; a module-level set
         of added names). Docstring names ADR 0278.
      4. tests/conftest.py: an autouse fixture `_restore_upstream_tool_tables` that calls
         upstream_mcp_registry.unregister_all() after each test.
      5. tests/unit/test_upstream_mcp_registry.py: after register, every table holds the expected
         value; TOOL_REGISTRY has the entry with its verb; ALL_OPERATIONS contains "kh.search";
         McpDispatcher.check_policy accepts kh_search (the tests/unit/web/test_mcp_dispatch.py
         construction); a scoped token naming kh_search passes the check at scoped_tokens.py:728;
         policy.propose.proposals_for offers nothing for kh.propose_change; a collision refuses with
         nothing written; unregister_all restores every table and both derived values exactly.
         tests/unit/policy/test_registry.py: rebuild_registry keeps the same object.
         tests/unit/policy/test_propose.py: refresh_all_operations picks up a new operation.
      6. Stop with status=blocked if a module other than these two holds an import-time copy of
         TOOL_TO_GATE, TOOL_TO_OPERATION, TOOL_TO_VERB or EFFECT_BY_TOOL that the step-5 tests show
         missing kh_search; name the module.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_registry.py tests/unit/policy/test_registry.py tests/unit/policy/test_propose.py -q passes
      - python3 -m pytest tests/unit -q passes
  - id: p6-reference-upstream
    title: A reference upstream MCP server that verifies claims and filters by domain
    depends_on: [p4-upstream-claims]
    complexity: M
    touches:
      - scripts/upstream_mcp_reference_server.py
      - tests/unit/test_upstream_mcp_reference_server.py
    brief: |
      Read first: docs/1091/plan.md §3.5, §3.9; src/privacyfence/web/routes_mcp.py (how the low-level mcp 2.x Server and its session manager are built); tests/unit/web/test_routes_mcp_principal.py (in-process client over ASGI)
      1. Create scripts/upstream_mcp_reference_server.py: a Starlette app with the mcp 2.x
         low-level Server over StreamableHTTPSessionManager at `/mcp`, built by
         `build_app(*, audience: str, issuer: str | None, jwks: dict | None = None, jwks_url: str | None = None,
         public_key_pem: bytes | None = None, documents: list[dict] | None = None) -> Starlette`;
         exactly one of jwks / jwks_url / public_key_pem. Every request needs `Authorization: Bearer
         <jwt>`: PyJWT, RS256 only, aud = audience, iss when given, exp/nbf with no leeway, header
         typ == "pf-upstream-claim+jwt", a jti seen before its exp is refused; any failure is HTTP
         401 `{"error": "invalid_claim"}`. Decoded claims reach the tool handlers through a
         contextvar.
      2. Tools: `search` (`query`: string) returns, as JSON text, the documents whose `domain` is in
         the claim's domains and whose title or body contains query (case-insensitive); `get_doc`
         (`id`: string) returns the document only when its domain is allowed, else the error result
         "not found"; `propose_change` (`id`, `text`: strings) returns `{"proposal": "accepted", "by":
         <sub>}`. A service claim lists tools and gets no documents. Default documents: four, titled
         "Policy overview" (general), "Office policy" (general), "HR policy" (hr), "Finance policy"
         (finance), each body containing "policy".
      3. `main()` with argparse: `--host` (default 127.0.0.1), `--port` (default 8099),
         `--audience` (required), `--issuer`, and exactly one of `--jwks-url`, `--jwks-file`,
         `--public-key-file`; prints `listening on <host>:<port>` and runs uvicorn. The docstring
         says it is a reference for upstream implementers and for tests, not a production server.
      4. tests/unit/test_upstream_mcp_reference_server.py drives build_app in process with the real
         mcp client over an httpx2 ASGI transport, claims minted by
         privacyfence.upstream_claims.ClaimSigner with a tmp_path key: search filters by domains;
         get_doc of the HR document without hr is "not found"; missing, expired, wrong-aud,
         wrong-typ and replayed claims get 401.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_reference_server.py -q passes
      - ruff check scripts/upstream_mcp_reference_server.py passes
  - id: p7-client-catalog
    title: The upstream MCP client and tool catalog
    depends_on: [p1-tool-input-schema, p6-reference-upstream]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_client.py
      - pyproject.toml
      - requirements/*.lock.txt
      - tests/unit/test_upstream_mcp_client.py
    brief: |
      Read first: docs/1091/plan.md §3.6 (first four bullets); sync/src/privacyfence_sync/client.py:40-80
      1. pyproject.toml: move `httpx2>=2.5` from the test extra to the runtime dependencies with
         the comment "mcp's client transport needs it for upstream MCP servers (ADR 0278)". Run
         scripts/update_dependency_locks.sh and commit the requirements/*.lock.txt it changes.
         Stop with status=blocked if the lock run adds a package that `git show
         HEAD:requirements/runtime.lock.txt` does not list.
      2. Create src/privacyfence/upstream_mcp_client.py with UpstreamMcpClient,
         UpstreamMcpClientError, CallResult and UpstreamToolCatalog exactly per §3.6 (strings
         verbatim). list_tools uses upstream_claims.service_caller(). Docstring names ADR 0278 and
         ADR 0279.
      3. tests/unit/test_upstream_mcp_client.py: run the p6 reference app in process as the
         transport (httpx2 ASGI transport passed as `transport=`): list_tools; call_tool with text
         and with structured results; the 1,000,000-character cap (kind too_large); an is_error
         result (kind tool_error, detail set); an unreachable URL (kind unreachable, use a closed
         loopback port without a transport); the catalog: description cleaning and 700 cap, the
         Returns sentence and the gate sentence per gate, `reason` in params and in input_schema
         properties and required, each skip reason including `$defs` nested two levels deep,
         missing(), file order kept.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_client.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_client.py -q --cov=privacyfence.upstream_mcp_client --cov-branch --cov-fail-under=100 passes
      - python3 -c "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb')); assert any(x.startswith('httpx2') for x in d['project']['dependencies'])" exits 0
  - id: p8-connector
    title: The upstream MCP connector and its catalog entries
    depends_on: [p5-tool-registration, p7-client-catalog]
    complexity: M
    touches:
      - src/privacyfence/connectors/upstream_mcp.py
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - tests/unit/connectors/test_upstream_mcp_connector.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/web/test_tool_schema_portability.py
      - tests/unit/test_systemic_gate_invariants.py
    brief: |
      Read first: docs/1091/plan.md §3.6 (connector bullet); docs/coding-and-testing-guidelines.md §1.4, §1.5, §2.6; src/privacyfence/connectors/peek.py (gated_call and _auto_audit); src/privacyfence/google_errors.py:35-50
      1. Create src/privacyfence/connectors/upstream_mcp.py with UpstreamMcpConnector,
         UpstreamUnavailableError and UpstreamToolError exactly per §3.6 (strings verbatim). The
         caller is obtained only from the `caller` callable; the module never imports
         current_principal, current_agent or current_token_scope. Docstring: the auto / review /
         popup handling and that connector code never reads the caller (ADR 0036, amended by ADR
         0279).
      2. scripts/pyinstaller_common.py: add `privacyfence.connectors.upstream_mcp` to the hidden
         imports list.
      3. scripts/generate_tools_reference.py: add `"upstream_mcp": "Upstream MCP servers"` to
         CONNECTOR_TITLES and the matching CONNECTOR_SHORT entry (`"Upstream MCP"`); run
         `python3 scripts/generate_tools_reference.py` to regenerate docs/tools-reference.md.
      4. tests/unit/connectors/test_readme_manifest_alignment.py: add UpstreamMcpConnector to its
         CONNECTOR_CLASSES list (:40). test_systemic_gate_invariants.py discovers classes itself
         (:74); add a class `TestUpstreamConnectorInvariants` there that registers a fake upstream
         (one auto, one review, one popup tool), builds the connector with a catalog updated from
         fake mcp Tool objects, and asserts the reason-param and audit-trail invariants on it.
      5. tests/unit/connectors/test_upstream_mcp_connector.py (§2.6): TestDispatch; the auto tool
         writes an audit row and never calls the gate; the review tool calls gated_call with the
         result as raw and filtered data and pii_scan_text; the popup tool calls gated_call before
         the client (assert call order) and a denial (gated_call raising) never reaches the client;
         preview holds argument names and no values; each UpstreamMcpClientError kind maps to its
         error type and message, and safe_errors.public_message returns that message;
         assert_all_tools_leave_an_audit_trail; TestToolDefinitions with
         assert_tool_definitions_complete(connector, {}) on the fake-catalog connector. In
         tests/unit/web/test_tool_schema_portability.py add one test running its schema checks on
         that connector's specs.
      6. Stop with status=blocked if test_readme_manifest_alignment.py or
         test_docs_tools_reference.py require README or table rows for a connector whose
         cls(MagicMock()) has no tools.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_upstream_mcp_connector.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_pyinstaller_hidden_imports.py tests/unit/test_docs_tools_reference.py tests/unit/test_connector_catalog.py tests/unit/test_connector_tool_annotations.py -q passes
      - grep -rn "current_principal\|current_agent\|current_token_scope" src/privacyfence/connectors/upstream_mcp.py prints nothing
  - id: p9-org-wiring
    title: Wire upstreams into the organization server, JWKS route and status
    depends_on: [p3-verified-email, p8-connector]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_runtime.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/web/server.py
      - src/privacyfence/web/routes_upstream_mcp.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/org_mode.py
      - src/privacyfence/settings_controller.py
      - tests/conftest.py
      - tests/unit/test_upstream_mcp_runtime.py
      - tests/unit/web/test_routes_upstream_mcp.py
      - tests/unit/test_daemon_main.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_server_org_mode.py
      - tests/integration/test_upstream_mcp_org.py
    brief: |
      Read first: docs/1091/plan.md §3.5, §3.7, §3.8; src/privacyfence/daemon_main.py:960-1010, :1495-1515, :1649-1700; src/privacyfence/web/server.py:1355-1390; src/privacyfence/web/routes_oidc.py
      1. Create src/privacyfence/upstream_mcp_runtime.py with the §3.8 functions. caller_for:
         principal = current_principal(); scope = current_token_scope(); without a scope,
         domains = grants.domains_for(upstream.id, principal.email, principal.email_verified) and
         the CallerClaim email/email_verified come from the principal; with a scope,
         domains = sorted(set(grants.domains_for(upstream.id, scope.principal_email,
         getattr(scope, "principal_email_verified", False))) & set(getattr(scope,
         "upstream_domains", {}).get(upstream.id, ()))) (p10 adds both fields); act =
         upstream_claims.actor_from_context(). connectors_for_principal builds one
         UpstreamMcpConnector per upstream with the shared catalog and client and
         `caller=functools.partial(caller_for, upstream)`. held_domains returns
         {id: frozenset(grants.domains_for(id, principal.email, email_verified))}.
      2. tests/conftest.py: in the same autouse fixture p5 added, also call upstream_mcp_runtime.reset().
      3. web/routes_upstream_mcp.py: `build_routes(signer) -> list[Route]`, GET
         `/upstream-mcp/jwks` returning signer.jwks() as JSON with `Cache-Control: max-age=300`.
      4. web/server.py _build_org_app: when upstream_mcp_runtime.configured(), append
         `upstream_mcp_runtime.lifespan(mcp_dispatcher.notify_tools_changed)` to its lifespans and
         extend extra_routes with routes_upstream_mcp.build_routes(...) beside routes_oidc (:1383).
      5. daemon_main.py: the local-mode refusal beside :974 and the organization-mode setup of
         §3.8; build_connectors appends upstream_mcp_runtime.connectors_for_principal() when
         configured().
      6. web/mcp_dispatch.py status(): `"upstreams": upstream_mcp_runtime.status()` only when
         configured(). org_mode.connector_enabled_by_org and settings_controller lookups keyed by
         connector name skip upstream_mcp_runtime.upstream_ids() (§3.8). Stop with status=blocked
         naming any other lookup that raises for an upstream id.
      7. Tests: runtime unit tests (caller_for for a verified person, an unverified person, a
         stand-in scoped token with and without upstream_domains; held_domains; status states;
         lifespan with a fake client failing twice then succeeding, notify called once, backoff
         delays patched); the JWKS route; route and lifespan mounted only when configured
         (tests/unit/web/test_server_org_mode.py); daemon refusal outside org mode; status key
         present and absent. tests/integration/test_upstream_mcp_org.py: a real org server on
         loopback (the `_running_org_server` pattern in tests/integration/test_browser_smoke.py,
         with tests/integration/mock_idp.py), an access token minted as
         tests/unit/web/test_server_org_mode.py:494 does for a verified principal with grants
         {general}, the p6 reference server on another loopback port verifying via the server's
         /upstream-mcp/jwks; the real mcp client lists `ref_search`, calls it and gets only general
         documents; privacyfence_status shows the upstream ready; a popup tool with the gate patched
         to deny never reaches the reference server.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_runtime.py tests/unit/web/test_routes_upstream_mcp.py tests/unit/test_daemon_main.py tests/unit/web/test_mcp_dispatch.py tests/unit/web/test_server_org_mode.py -q passes
      - python3 -m pytest tests/integration -q passes
  - id: p10-scoped-token-domains
    title: Scoped tokens carry per-upstream domains, set in Settings
    depends_on: [p9-org-wiring]
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/scoped_token_proposals.py
      - src/privacyfence/web/mcp_auth.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/server.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/unit/web/test_mcp_auth.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_server.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_settings_scoped_token_form_browser.py
    brief: |
      Read first: docs/1091/plan.md §3.7; docs/adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md; docs/adr/0236-a-scoped-token-holds-its-own-rules.md; docs/adr/0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md
      1. scoped_tokens.py: the two ScopedToken fields, serialized only when non-default like
         allow_personal_data_reads (:219, :246, :310); normalise_create_request (:205) normalises
         `upstream_domains`; ScopedTokenStore.create (:595) takes the three new keyword arguments of
         §3.7 and refuses an unheld domain with the §3.7 message, using the store's existing error
         type. No import of upstream_mcp_runtime here.
      2. web/mcp_auth.py ScopedTokenVerifier: add `"email_verified": found.principal_email_verified`.
      3. web/routes_settings.py create-token route: pass `upstream_domains` from the normalised
         request, `principal_email_verified` from the browser session's `email_verified`
         (web/org_session.py:54), and `held_domains=upstream_mcp_runtime.held_domains(principal,
         session.email_verified)` when configured(), else `{}`.
      4. web/server.py, the Settings payload listing token tools (:387): add `upstreams: [{"id",
         "label", "tools": [exposed names], "domains": [the person's held domains]}]` when configured().
      5. settings_window_html.py: the §3.7 fieldset and hint in the New token form, shown for an
         upstream when one of its tools is ticked; unticked domains are not sent.
      6. scoped_token_proposals.py: refuse a proposal naming any tool of upstream_mcp_runtime's
         upstreams with the §3.7 message (lazy import inside the function).
      7. Tests: store round-trip with and without the fields (an old file loads); the verifier
         claim; normalisation and the step-up fingerprint changing with domains; refusal of an
         unheld domain; the payload; the form markup; the proposal refusal; the browser test ticks
         one domain and the created token holds exactly it.
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_scoped_token_proposals.py tests/unit/web/test_mcp_auth.py tests/unit/web/test_routes_settings.py tests/unit/web/test_server.py tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/integration -q passes, with test_settings_scoped_token_form_browser.py not skipped
  - id: p11-docs-and-adrs
    title: Operator and implementer docs, ADRs 0278-0280, changelog
    depends_on: [p10-scoped-token-domains]
    complexity: S
    touches:
      - docs/upstream-mcp.md
      - docs/adr/0278-upstream-mcp-servers-are-declared-connectors.md
      - docs/adr/0279-privacyfence-sends-the-upstream-a-signed-caller-claim.md
      - docs/adr/0280-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md
      - docs/adr/README.md
      - docs/configuration-reference.md
      - docs/org-mode-setup-guide.md
      - docs/testing-policy.md
      - docs/README.md
      - CHANGELOG.md
    brief: |
      Read first: docs/1091/plan.md §3, §4, §5; docs/adr/README.md (template and index format); CONTRIBUTING.md "Decisions, plans and ADRs"
      1. docs/upstream-mcp.md: title and a 3-line summary. For operators: the file format (§3.2
         example and every rule), grants (§3.3, including what "verified" means), what a person
         and a scoped token see (§3.7), privacyfence_status, that file changes need a restart and
         grant edits do not, and the warning "Declaring a writing tool with a read verb skips the
         approval card." For upstream implementers: verify RS256 against /upstream-mcp/jwks;
         require typ, aud, iss, exp; keep a jti cache; only sub, domains and act.type may change
         results, the rest is audit-only; the service actor; a Postgres row-level-security sketch
         (`SET LOCAL app.domains = '<comma list>'` per transaction and a policy
         `USING (domain = ANY (string_to_array(current_setting('app.domains'), ',')))`). A
         "Reference upstream" section: the `ref` operator file used by ma1 (url
         `http://refup:8099/mcp`, audience `ref-upstream`, tools search/get_doc auto, propose_change
         popup with an effect), the two `docker exec -i privacyfence python3 -` commands ma1 steps 7
         and 9 run (each loads
         /var/lib/privacyfence-org/.privacyfence/org/admin_config.json, sets
         `upstream_mcp.ref.grants`, writes it back), and how to run
         scripts/upstream_mcp_reference_server.py.
      2. ADRs 0278, 0279, 0280 per §4: Status Accepted with today's date, Context, Decision,
         Alternatives considered (from §3's "Rejected" lines), Consequences, Verification (the tests
         of p2-p10). 0279's Status says it amends ADR 0036; 0280's says it amends 0164 and 0236.
         Add their rows to docs/adr/README.md in the index format.
      3. docs/configuration-reference.md: PRIVACYFENCE_UPSTREAM_MCP_FILE and the admin_config
         `upstream_mcp` key, linking docs/upstream-mcp.md. docs/org-mode-setup-guide.md: one
         paragraph linking it. docs/testing-policy.md, layer 5: one line that a connector with no
         external provider (the upstream MCP connector) has no live fixture and is covered by the
         reference server (ADR 0278). docs/README.md: a row for docs/upstream-mcp.md in its format.
      4. CHANGELOG.md under "## [Unreleased]": an "Added" entry for upstream MCP servers behind
         PrivacyFence with signed caller claims and domain grants, and one for scoped-token data
         domains. No version heading.
    acceptance:
      - ls docs/adr/0278-*.md docs/adr/0279-*.md docs/adr/0280-*.md succeeds
      - grep -c "0278\|0279\|0280" docs/adr/README.md prints 3 or more
      - grep -n "PRIVACYFENCE_UPSTREAM_MCP_FILE" docs/configuration-reference.md docs/upstream-mcp.md matches
      - python3 -m pytest tests/unit -q passes
```
