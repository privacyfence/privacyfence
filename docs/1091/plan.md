# Upstream MCP connector: tool policy in PrivacyFence, signed caller claims to the upstream

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator of an organization server writes a small JSON file that names a remote MCP
server (for example a knowledge hub), lists which of its tools PrivacyFence exposes and how each is
gated, and points `PRIVACYFENCE_UPSTREAM_MCP_FILE` at it. An administrator adds domain grants to
`admin_config.json` (who may see which data domains). AI clients then see tools such as `kh_search`
and `kh_propose_change`; reads declared `auto` run at once, `popup` writes show an approval card, and
every call PrivacyFence forwards carries a short-lived PrivacyFence-signed JWT that names the person,
their allowed domains and the calling client or workflow. The upstream verifies it against
`/upstream-mcp/jwks` and filters its own queries.

**Scope:** New: the upstream connector, its config file and grants, the claim signer and JWKS route,
a `ToolSpec.input_schema` pass-through, startup registration of declared tools into the policy
tables, `email_verified` on the MCP principal, and per-upstream domain narrowing on scoped tokens
(Settings form). Not changed: local (desktop) mode, which refuses the file; no auto-accept rules or
"Always allow" for upstream tools in this release; nothing filters upstream results by data domain
(the upstream does); the knowledge hub's own claim verification and row-level security live in its
own repository.

**Security:** The claim moves identity across a trust boundary that ADR 0036 kept closed: it is built
at the transport, never by connector code, carries an agent name only when attested, and grants
match only a verified email. Writes must be declared `popup`; a scoped token never widens domains
and a missing entry gives none. A separate RS256 key with an explicit `typ` keeps claims and OIDC ID
tokens apart.

**Performance:** Each upstream tool call opens one short MCP client session (initialize, call); the
tool list is fetched once at startup in the background, with no periodic refresh.

**Review focus:** Startup mutation of the module-level policy tables (§3.4); the claim's actor and
service shapes and ADR 0006/0036 (§3.5); verified-email domain grants and scoped-token narrowing
(§3.3, §3.7); the declared-gate rules that forbid an ungated write (§3.2).

## 1. Goal

Issue [#1091](https://github.com/privacyfence/privacyfence/issues/1091). PrivacyFence becomes a
policy-enforcing proxy in front of any Streamable-HTTP MCP server ("upstream"). Tool-level policy
(which tools exist for a caller, which need approval) is enforced by PrivacyFence from the tool name
and arguments, exactly like a built-in connector, with the usual cards, scoped tokens, unattended
denial and audit log. Data-level policy is not done in PrivacyFence: it resolves who the caller is
and which data domains they may see, and sends that as a signed claim on every upstream request, so
the upstream applies it inside its own queries (ranking and pagination stay correct, a guessed
document id is checked where the data lives). PrivacyFence stays the single place for identity,
policy decisions, approval and audit; the upstream trusts only PrivacyFence-signed claims and holds
no credentials or approvals.

Owner decisions (board ask `a-f3eba9a5d206e92e`, 2026-11-13): tools are **declared** by the
operator; domains come from **admin-config grants**, narrowed by scoped tokens; **organization
servers only**; the claim has `sub` = the person and an **actor** of `ai_client` or `workflow`.

## 2. Current state

- **Tools are static.** `ToolSpec`/`ToolParam` (`src/privacyfence/connector.py:16-75`) carry flat
  scalar params only; `web/mcp_tools.py:73-100` builds every JSON schema from them.
  `connector_catalog.py:39` builds `cls(None).tool_specs()`, and `tool_specs()` is synchronous and
  called on every request (`web/routes_mcp.py:449-454`, `:576-581`; `web/mcp_dispatch.py:240`,
  `:377`). Nothing discovers tools at runtime; `McpDispatcher.notify_tools_changed`
  (`web/mcp_dispatch.py:216`) can announce a change.
- **Policy tables are module-level dicts keyed by tool name:** `auto_accept.TOOL_TO_GATE`
  (`auto_accept.py:218`), `auto_accept.TOOL_TO_OPERATION` (`:86`), `policy/registry.TOOL_TO_VERB`
  (`policy/registry.py:125`), `write_effects.EFFECT_BY_TOOL` (`write_effects.py:42`),
  `gate._TOOL_LAYOUT` (`gate.py:262`). Two values are derived at import:
  `policy/registry.TOOL_REGISTRY` (`policy/registry.py:270`) and `policy/propose.ALL_OPERATIONS`
  (`policy/propose.py:91`). Consumers that refuse an unknown tool: `mcp_dispatch.check_policy`
  (`web/mcp_dispatch.py:391`), `scoped_tokens.py:728`, `scoped_token_proposals.py:198`, the Settings
  tool list (`web/server.py:387`).
- **Gating lives in the connector**: each tool calls `gate.gated_call` (`gate.py:940`) or writes its
  own audit row (`connectors/peek.py:889` `_auto_audit`). Rules are keyed by operation and a scope
  selector (`policy/scopes.py`); "Always allow" offers only scopes in `policy/propose.PROPOSABLE_SCOPES`
  (ADR 0077), so an operation with no scope gets no rule offer.
- **The caller** is three contextvars set around dispatch (`web/routes_mcp.py:503-504`):
  `current_principal()` (`principal.py:97`; `Principal(id, email, display_name, is_admin)`, with
  `email` documented as cosmetic, `principal.py:49`), `current_agent()` (`agent_identity.py:259`;
  `is_attested()` true only for `OVERRIDE`/`OAUTH_CLIENT`, `:55`) and `current_token_scope()`
  (`scoped_tokens.py:974`). Connectors receive only `(tool, args)` (ADR 0036).
- **Verified email is not on the MCP principal.** `OrgOAuthProvider._mint_tokens`
  (`web/oauth_provider.py:802`) stores `email`, `display_name`, `is_admin` in the access-token
  claims and in `SealedRefreshRecord` (`web/sealed_refresh_store.py:119`);
  `principal_from_access_token` (`web/mcp_auth.py:280`) rebuilds the principal from them.
  `org_identity.session_email_verified` (`org_identity.py:490`) computes the verified bit for the
  browser session only. `ScopedTokenVerifier` (`web/mcp_auth.py:225-245`) copies the owner's
  email from the token record.
- **Signing exists for OIDC only:** `oidc_provider.load_or_create_signing_key(path)` and `jwks(key)`
  (`oidc_provider.py:177`, `:198`; RS256, RSA 2048, mode 0600, ADR 0274), loaded only when
  `PRIVACYFENCE_OIDC_CLIENTS` is set (`daemon_main.py:1503-1510`); `/oidc/jwks` is mounted at
  `web/server.py:1383`. PyJWT and `cryptography` are runtime dependencies.
- **No outbound MCP client in `src/`.** `sync/src/privacyfence_sync/client.py:50-70` shows the
  pattern (`streamable_http_client(url, http_client=httpx2.AsyncClient(...))`). `mcp` is locked at
  2.3.0; `httpx2` is a dev dependency of the main package (`pyproject.toml:250-260`) and already in
  `requirements/runtime.lock.txt` as a dependency of `mcp`.
- **Mode guard pattern:** `daemon_main.py:974-978` refuses `PRIVACYFENCE_OIDC_CLIENTS` outside
  organization mode.

## 3. Design

### 3.1 Names

| Thing | Name |
|---|---|
| Config file variable | `PRIVACYFENCE_UPSTREAM_MCP_FILE` |
| Config module | `src/privacyfence/upstream_mcp_config.py` |
| Claim module | `src/privacyfence/upstream_claims.py` |
| Table registration | `src/privacyfence/upstream_mcp_registry.py` |
| MCP client | `src/privacyfence/upstream_mcp_client.py` (`UpstreamMcpClient`, `UpstreamMcpClientError`, `UpstreamMcpToolError`) |
| Connector | `src/privacyfence/connectors/upstream_mcp.py` (`UpstreamMcpConnector`) |
| Startup wiring | `src/privacyfence/upstream_mcp_runtime.py` |
| JWKS route | `src/privacyfence/web/routes_upstream_mcp.py`, path `/upstream-mcp/jwks` |
| Signing key file | `org_dir() / "upstream_claim_signing_key.pem"` |
| Reference upstream | `scripts/upstream_mcp_reference_server.py` |
| Operator and implementer doc | `docs/upstream-mcp.md` |

### 3.2 The operator file

JSON, read once at startup. Any error raises `org_mode.ConfigurationError` and refuses the start,
naming the file and the JSON path of the bad value (never a value itself).

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

Rules (each failure message starts `PRIVACYFENCE_UPSTREAM_MCP_FILE: `):

- `version` must be `1`. Top-level keys other than `version`, `upstreams` are refused; so are unknown
  keys in an upstream or a tool. At most 8 upstreams, at most 50 tools per upstream.
- `id` matches `^[a-z][a-z0-9]{1,15}$`; ids are unique; an id is refused when it equals, is a prefix
  of, or has as a prefix any built-in connector name (the keys of `org_mode.CONNECTOR_ORG_SECTION`
  plus `"privacyfence"`), compared with a trailing `_` on both sides (so `kh` vs `kh2` is fine, `gmail`
  vs `gmai` is refused only if one is a prefix of the other's `name + "_"`). Message:
  `upstream id "<id>" collides with the built-in connector "<name>"`.
- `label`: 1-60 characters, no control characters.
- `url`: `https://` any host, or `http://` only when the host is `localhost`, a loopback IP
  literal, or a single-label name with no dot (a container on the suite network, e.g.
  `http://knowledge-hub:8080/mcp`). No userinfo, no fragment.
- `audience`: required, 1-200 characters, no whitespace; compared exactly by the upstream.
- Tool names match `^[A-Za-z0-9_-]{1,48}$`; the exposed name `<id>_<tool>` is at most 64 characters.
- `gate` is one of `auto`, `review`, `popup`; `verb` is a `policy.registry.Verb` value
  (`read`, `download`, `search`, `add`, `update`, `restructure`, `move`, `send`, `delete`).
- **Read verbs** (`read`, `download`, `search`) take `gate` `auto` or `review`; **every other verb**
  takes `gate` `popup` and must carry `effect` (1-200 characters, one sentence ending in `.`).
  `effect` on a read verb is refused. So an undeclared or ungated write cannot exist.
- `ToolSpec` derived: `read_only = verb in {read, download, search}`, `destructive = verb == "delete"`.

Dataclasses (frozen) in `upstream_mcp_config.py`:

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

In `admin_config.json`, top-level key `upstream_mcp` (default `{}` in
`admin_config._default_config`):

```json
"upstream_mcp": {
  "kh": {"grants": [
    {"match": "*",                 "domains": ["general"]},
    {"match": "@hr.example.com",   "domains": ["hr"]},
    {"match": "ceo@example.com",   "domains": ["hr", "finance"]}
  ]}
}
```

- `match` is `*` (any verified person), `@<domain>` (exact, case-insensitive email domain) or an
  exact case-insensitive email. Domain names match `^[a-z0-9][a-z0-9_-]{0,62}$`. At most 200 grants
  per upstream. A grant for an upstream id the operator file does not declare is logged once as a
  warning and ignored. An invalid section refuses the start with `ConfigurationError`
  (`admin_config.json: upstream_mcp.<id>.grants[<n>]: <problem>`).
- `domains_for(upstream_id, email, email_verified) -> tuple[str, ...]`: empty when
  `email_verified` is false or `email` is empty; otherwise the sorted union of every matching grant.
- `GrantStore(path)` in `upstream_mcp_config.py` re-reads the file when its `st_mtime_ns` changes
  (one `stat` per call), so an administrator's edit applies to the next call without a restart; a
  re-read that fails validation keeps the last good grants and logs an error.
- There is no Admin page control in this release; it is a file edit (documented).

### 3.4 Registering declared tools

`upstream_mcp_registry.register(upstreams)` runs once at startup, before the web server serves,
and writes, for every `DeclaredTool`:

- `auto_accept.TOOL_TO_GATE[exposed] = gate`
- `auto_accept.TOOL_TO_OPERATION[exposed] = operation`
- `policy.registry.TOOL_TO_VERB[exposed] = Verb(verb)`
- `write_effects.EFFECT_BY_TOOL[exposed] = effect` (popup only)
- `gate._TOOL_LAYOUT[exposed] = gate.WIDE` for `review` and `popup`

then calls `policy.registry.rebuild_registry()` (new: `TOOL_REGISTRY.clear()` then
`TOOL_REGISTRY.update(_build_registry())`, in place, because other modules imported the object) and
`policy.propose.refresh_all_operations()` (new: rebinds the module global `ALL_OPERATIONS` from
`TOOL_REGISTRY`; `personal_settings.py:275` keeps reading it as an attribute). An exposed name
already present in `TOOL_TO_GATE` raises `ConfigurationError("PRIVACYFENCE_UPSTREAM_MCP_FILE: tool
\"<exposed>\" collides with an existing tool")` before anything is written. `unregister_all()`
removes every name `register` added (kept in a module-level set) and rebuilds the same two derived
values; tests use it. No scope selector, catalogue entry or proposable scope is added, so upstream
tools get no "Always allow" offer and no rule (ADR 0077's mechanism); rules for them are a later
change.

Rejected: lookup functions replacing direct dict access in ~15 modules (large cross-cutting diff
for tools that change only at restart).

### 3.5 The claim

A JWT, RS256, signed with a key of its own (`load_or_create_signing_key(org_dir() /
"upstream_claim_signing_key.pem")`), published at `/upstream-mcp/jwks` (the `jwks(key)` output).
Header: `{"alg": "RS256", "kid": <key.kid>, "typ": "pf-upstream-claim+jwt"}`. Payload:

| Claim | Value |
|---|---|
| `iss` | `<issuer_url without trailing slash>/upstream-mcp` |
| `aud` | the upstream's `audience` |
| `sub` | `principal.id`; `"privacyfence"` for a service claim |
| `email` | `principal.email` when `principal.email_verified`, else `""` |
| `email_verified` | `principal.email_verified` |
| `domains` | sorted list from §3.3 / §3.7 |
| `act` | see below |
| `pf_upstream` | upstream id |
| `pf_tool` | the upstream tool name for `tools/call`, `null` otherwise |
| `iat`, `nbf` | now (integer seconds) |
| `exp` | `iat + 60` |
| `jti` | `uuid.uuid4().hex` |

`act`:
- scoped token in scope (`current_token_scope()` not `None`, checked first):
  `{"type": "workflow", "token_id": scope.id, "label": scope.label}`.
- otherwise `{"type": "ai_client", "attested": agent.is_attested(), "name": <agent.name if
  attested else "">, "claimed_name": <agent.name if not attested else "">}`.
- service claim (the startup `tools/list`, no person): `{"type": "service"}`, `sub`
  `"privacyfence"`, `email` `""`, `email_verified` false, `domains` `[]`.

The service actor is a third value beyond the owner's two; it carries no person and no domains, so
an upstream following the contract returns no data for it. `docs/upstream-mcp.md` tells
implementers that only `sub`, `domains` and `act.type` may change what a query returns; `name`,
`claimed_name` and `label` are for their audit only (ADR 0006 decision 3).

`upstream_claims.py`:

```python
CLAIM_TYPE = "pf-upstream-claim+jwt"
CLAIM_LIFETIME_SECONDS = 60
KEY_FILENAME = "upstream_claim_signing_key.pem"

@dataclass(frozen=True)
class CallerClaim:          # what the claim says about the caller
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

def actor_from_context() -> dict[str, object]: ...       # reads current_token_scope(), current_agent()
def service_caller() -> CallerClaim: ...

class ClaimAuth(httpx2.Auth):   # mints a fresh JWT per HTTP request (own jti), sets
    ...                         # "Authorization: Bearer <jwt>"
```

The connector body never reads the three contextvars: the connector asks
`upstream_mcp_runtime.caller_for(upstream)` (§3.8), which builds the `CallerClaim` beside the
transport. Rejected: reusing the OIDC key (ties the feature to `PRIVACYFENCE_OIDC_CLIENTS`, lets a
claim be confused with a Peek/Grist ID token on one JWKS, couples rotation); a custom header
(MCP servers already verify bearer tokens); a longer lifetime or claim caching (a revoked grant
must stop within a minute).

### 3.6 Client, connector and tool specs

- `UpstreamMcpClient(upstream, signer)`: `async list_tools() -> list[mcp.types.Tool]` (service
  claim) and `async call_tool(tool, arguments, caller) -> CallResult`. Each call opens its own
  `httpx2.AsyncClient(auth=ClaimAuth(...), timeout=httpx2.Timeout(60.0, connect=10.0))`,
  `streamable_http_client(upstream.url, http_client=...)`, `ClientSession.initialize()`, one
  request, close. Never shared across calls or principals. Transport failures raise
  `UpstreamMcpClientError(f"The {upstream.label} server could not be reached.")`.
- `CallResult`: the text of every `TextContent` block joined with `"\n"`, plus
  `structured_content` (dict or `None`); other content blocks are dropped and counted. Text over
  1,000,000 characters raises `UpstreamMcpClientError(f"The {upstream.label} result is larger than
  1 MB; ask for less.")`. A result with `is_error` raises `UpstreamMcpToolError(f"The
  {upstream.label} server returned an error: {text[:2000]}")`. Both error types join the
  `safe_errors.public_message` allowlist.
- `UpstreamToolCatalog` (in `upstream_mcp_client.py`): holds the last successful `list_tools()`
  snapshot and builds `ToolSpec`s for the declared tools that the snapshot contains, in file order:
  - `name` = exposed name; `description` = the upstream description with control characters
    removed, cut to 800 characters, then one approval sentence: `auto`: none; `review`:
    ` PrivacyFence shows the result to the person for approval before it is returned.`; `popup`:
    ` PrivacyFence asks the person to approve this call before it is sent.`
  - `input_schema` = the upstream `inputSchema` (deep copy) with a required `reason` property added:
    `{"type": "string", "description": "One sentence: why are you calling this tool right now?"}`.
    A tool is skipped (logged at WARNING with the exposed name) when its schema is not a dict with
    `"type": "object"`, already has a `reason` property, serializes to more than 32 KB, or has a
    `$ref` not starting with `#`.
  - `read_only`, `destructive` from §3.2.
  - `missing()` returns declared tools the snapshot lacks or that were skipped.
- `UpstreamMcpConnector(upstream=None, catalog=None, client=None)`: `name` is `upstream.id`
  (`"upstream_mcp"` when built with no upstream, so `connector_catalog`'s `cls(None)` works and
  yields `[]`); `tool_specs()` returns `catalog.specs()` or `[]`. `call(tool, args)`: unknown name
  raises `ValueError(f"Unknown upstream tool: {tool!r}")`; `reason` was already removed by the
  dispatcher.
  - `auto`: call upstream, write an `auto_accepted` audit row (the `connectors/peek.py:889`
    `_auto_audit` pattern, `auto_accept_rule="auto"`), return the result.
  - `review`: call upstream, then `return await gated_call(..., gate="review", raw_data=result,
    filtered_data=result, pii_scan_text=text, details_text=text, ...)`.
  - `popup`: `await gated_call(..., gate="popup", raw_data=args, filtered_data=None,
    details_text=json.dumps(args, indent=2, sort_keys=True), write_content_scan_text=<same>,
    args=args)` first, then call upstream and return the result.
  - Every gated call: `tool_name` = `f"{upstream.label}: {tool}"`, `summary` = `f"{verb} on
    {upstream.label}"` (verb capitalised), `sender` = `upstream.label`, `preview` =
    `{"Server": upstream.label, "Tool": tool, "Arguments": ", ".join(sorted(args)) or "none"}`
    (metadata only, never values).
  - Returned value: `structured_content` when it is a dict, else the text.

### 3.7 Scoped tokens narrow domains

- `ScopedToken` gains `upstream_domains: Mapping[str, frozenset[str]]` (default empty) and
  `principal_email_verified: bool` (default `False`), serialized only when non-default (the
  `allow_personal_data_reads` pattern, `scoped_tokens.py:219`, `:246`, `:310`), with no file-version
  bump. `principal_email_verified` is copied from the creating browser session.
- For a scoped call, domains = `domains_for(id, owner email, token.principal_email_verified) ∩
  token.upstream_domains.get(id, ∅)`. A token never widens; a missing entry gives no domains;
  tokens created before this change therefore send `domains: []` until recreated.
- Settings "New token" form: when the chosen tools include an upstream's tools, one checkbox per
  domain the person currently holds for that upstream (none checked by default). The create
  request carries `upstream_domains` normalised (sorted lists), so the step-up fingerprint covers it.
- AI token proposals (`scoped_token_proposals.py`, ADR 0250) refuse upstream tools in this release:
  `"Upstream tools cannot be proposed; create the token in Settings."`

### 3.8 Startup wiring (organization mode)

`upstream_mcp_runtime.py` holds the process-wide state:

```python
def configure(*, upstreams, signer, grants: GrantStore) -> None: ...
def configured() -> bool: ...
def connectors_for_principal() -> list[UpstreamMcpConnector]: ...
def caller_for(upstream: UpstreamServer) -> CallerClaim: ...   # principal + token scope + agent -> CallerClaim
def status() -> list[dict[str, object]]: ...                     # privacyfence_status entries
def lifespan(notify_tools_changed: Callable[[], None]): ...      # background tools/list fetch
```

- `daemon_main`: outside organization mode, a non-empty `PRIVACYFENCE_UPSTREAM_MCP_FILE` raises
  `ConfigurationError("PRIVACYFENCE_UPSTREAM_MCP_FILE is set, but this is not an organization
  server.")` (beside `daemon_main.py:974`). In organization mode with the variable set: load the
  file, validate grants, `register()`, load the key, `configure()`, append
  `connectors_for_principal()` in `build_connectors`, add `lifespan(...)` to the web server's
  lifespans, mount the JWKS route. Without the variable nothing changes and `/upstream-mcp/jwks`
  is not mounted.
- `lifespan`: one task per upstream; `list_tools()` with backoff 5 s doubling to 300 s until the
  first success, then `notify_tools_changed()` once. No periodic refresh (a changing description
  mid-session is a rug-pull vector; changes take effect on restart).
- `privacyfence_status` gains `upstreams`: `[{"id", "label", "tools_exposed", "state":
  "ready"|"waiting_for_tool_list", "missing_tools": [...]}]`.

### 3.9 Testing approach

No live provider: there is no OAuth sign-in and no third-party API to drift, so the two-PR
connect/tools procedure and the layer-5 live fixture (`docs/coding-and-testing-guidelines.md` §3,
`docs/testing-policy.md` "Layer 5") do not apply; `scripts/qa_fixture_recorder.py` does not list
this connector. Instead `scripts/upstream_mcp_reference_server.py` is a small upstream that verifies
claims (JWKS URL or PEM file, `typ`, `aud`, `iss`, `exp`, a `jti` replay cache) and serves `search`,
`get_doc`, `propose_change` over an in-memory document set with a `domain` field, filtering by the
claim's `domains`. Unit tests drive it in process; the integration test runs it on loopback against
a real organization daemon.

## 4. ADRs

- **0278** — Upstream MCP servers are declared connectors: the operator file, `<id>_<tool>`
  naming, gates fixed by verb (writes are always `popup`), startup registration into the policy
  tables, no rules for upstream tools yet, organization servers only, and no live fixture (layer 5
  does not apply). Relates to 0077, 0115, 0196.
- **0279** — PrivacyFence sends the upstream a signed caller claim: separate RS256 key and
  `/upstream-mcp/jwks`, `typ` `pf-upstream-claim+jwt`, the claim set, 60-second lifetime, one claim
  per HTTP request, the `act` shapes including `service`, built at the transport. Amends 0036;
  relates to 0006, 0273, 0274.
- **0280** — Data domains come from admin-config grants on a verified email, and a scoped token can
  only narrow them; `email_verified` is carried on the MCP principal. Amends 0164 and 0236; relates
  to 0272.

## 5. Manual steps

**Before implementation:** none.

**After implementation**

1. **ma1-reference-upstream-real-client** (about 25 minutes) — checks what CI cannot: a real AI
   client accepts the passed-through schemas and the claim reaches the upstream.
   1. On the QA suite server, start the reference upstream from the feature branch checkout:
      `python3 scripts/upstream_mcp_reference_server.py --port 8099 --jwks-url
      http://localhost:<privacyfence port>/upstream-mcp/jwks --audience ref-upstream`
      (see [docs/upstream-mcp.md](https://github.com/privacyfence/privacyfence/blob/feature/upstream-mcp-connector/docs/upstream-mcp.md)).
      You see `listening on 127.0.0.1:8099`.
   2. Write `/srv/privacyfence/upstream_mcp.json` with the example from that doc (`id` `ref`, `url`
      `http://localhost:8099/mcp`, `audience` `ref-upstream`), and add
      `PRIVACYFENCE_UPSTREAM_MCP_FILE=/srv/privacyfence/upstream_mcp.json` to the server's
      `privacyfence.env`.
   3. Add to `admin_config.json`: `"upstream_mcp": {"ref": {"grants": [{"match": "*", "domains":
      ["general"]}, {"match": "<your QA email>", "domains": ["general", "hr"]}]}}`. Restart the
      server.
   4. In claude.ai connected to the QA server, ask it to call `ref_search` for `policy`. You see
      results from the `general` and `hr` domains, with no approval card.
   5. Remove your own grant line (keep `*`), wait a few seconds, run the same search: only `general`
      results.
   6. Ask it to call `ref_propose_change`. An approval card titled `Reference upstream:
      propose_change` appears; approve it; the reply confirms the proposal.
   Report back: `ma1: pass` or `ma1: fail at step <n>: <what you saw>`.

## 6. Risks and open questions

- **Mutating module-level tables** (§3.4): a consumer that cached a value derived from them at
  import would miss upstream tools. A phase that sees a consumer other than `TOOL_REGISTRY` and
  `ALL_OPERATIONS` holding an import-time copy stops with `status=blocked` and names it.
- **Static checks do not see dynamic tools:** `assert_tool_definitions_complete` and
  `tests/unit/test_systemic_gate_invariants.py` run on `cls(None)` (no tools). p7 runs both on a
  connector built from a fake catalog.
- **Schema portability (ADR 0115):** upstream schemas may use `enum` or nested types some clients
  reject; ma1 checks one real client; this is documented as the operator's responsibility.
- **The upstream must verify `typ`, `aud`, `iss`, `exp` and `jti`.** PrivacyFence cannot enforce
  that; the doc and the reference server are the contract.
- **Review gate order:** a `review` read queries the upstream before approval, like every
  connector; an operator who declares a writing tool with a read verb bypasses the popup. The doc
  says so; the verb rule removes the accidental case only.
- **Old refresh chains carry no `email_verified`** and resolve as unverified (no domains) until the
  person signs in again: fail-closed by design.
- **`self._policy` in `OrgOAuthProvider`** must be the `AuthzPolicyConfig` that
  `session_email_verified` takes; if it is a different type, p3 stops with `status=blocked`.
- **CONTRIBUTING.md says plans are deleted when work lands**; the tld profile keeps
  `docs/<n>/plan.md` as a historical record. This plan follows the profile; the ADRs carry every
  decision either way.
- **Audit correlation** with the upstream (logging `jti` in PrivacyFence's audit row) is left out:
  it changes the audit entry format, which is chained (ADR 0071). A later change.

## Implementation manifest

```yaml
plan_slug: upstream-mcp-connector
feature_branch: feature/upstream-mcp-connector
tracking_issue: 1091
max_parallel: 3
manual_before: []
manual_after:
  - id: ma1-reference-upstream-real-client
    title: Run the reference upstream beside the QA suite server and call it from claude.ai as two grant sets
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
      Read first: docs/1091/plan.md §3.6, docs/coding-and-testing-guidelines.md §1.5, docs/adr/0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md
      1. In src/privacyfence/connector.py add to ToolSpec, after `destructive`, the field
         `input_schema: dict[str, Any] | None = None` with a comment: "A complete JSON Schema used
         verbatim instead of one built from params (upstream MCP tools, docs/1091/plan.md §3.6)."
      2. ToolSpec.to_dict adds `"input_schema": self.input_schema`; from_dict reads
         `input_schema=d.get("input_schema")`.
      3. In src/privacyfence/web/mcp_tools.py, tool_input_schema(spec) returns
         `copy.deepcopy(spec.input_schema)` when it is not None, before building from params.
      4. Tests: in tests/unit/test_connector.py a round-trip test of to_dict/from_dict with and
         without input_schema; in tests/unit/web/test_mcp_tools.py
         `test_tool_input_schema_uses_raw_schema_verbatim` (nested object and array survive) and
         `test_tool_input_schema_returns_a_copy` (mutating the result leaves the spec unchanged).
      5. Run `python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q` (the
         dod_conditional rule for connector.py). Stop with status=blocked if any code outside
         mcp_tools.py rebuilds a schema from spec.params for the MCP tool list.
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
      Read first: docs/1091/plan.md §3.1, §3.2, §3.3; src/privacyfence/oidc_provider.py:56-136 (the env-driven config pattern); src/privacyfence/admin_config.py:116-215
      1. Create src/privacyfence/upstream_mcp_config.py with the module docstring summarising
         §3.2-§3.3, the dataclasses DeclaredTool and UpstreamServer and the functions
         load_upstreams(path) and parse_upstreams(data, *, source) exactly as §3.2 gives them, plus
         `ENV_VAR = "PRIVACYFENCE_UPSTREAM_MCP_FILE"`, `READ_VERBS = frozenset({"read", "download",
         "search"})`, and `BUILTIN_NAMES` = keys of org_mode.CONNECTOR_ORG_SECTION plus
         "privacyfence". Implement every rule in §3.2 in that order; every error is
         `org_mode.ConfigurationError` whose message starts `PRIVACYFENCE_UPSTREAM_MCP_FILE: ` and
         names the JSON path (for example `upstreams[0].tools.search.gate`), never the value. The
         collision rule: refuse when `(id + "_").startswith(name + "_")` or
         `(name + "_").startswith(id + "_")` or either is a prefix of the other followed by "_".
         Verb values come from policy.registry.Verb.
      2. In the same module add `parse_grants(section: object, upstream_ids: set[str]) ->
         dict[str, tuple[Grant, ...]]` with `@dataclass(frozen=True) class Grant: match: str;
         domains: tuple[str, ...]`, the pure function `domains_for(grants, upstream_id, email,
         email_verified) -> tuple[str, ...]` and the class GrantStore(path) with
         `domains_for(upstream_id, email, email_verified)`, all per §3.3 (mtime re-read, keep last
         good grants on a bad re-read, error message format, unknown upstream ids warned once and
         ignored). GrantStore reads the `upstream_mcp` key of the JSON file at path; a missing
         key or file means no grants.
      3. In src/privacyfence/admin_config.py add `"upstream_mcp": {}` to _default_config's result
         so a loaded config always has the key; make sure _fill accepts arbitrary upstream ids
         under it (it is a free-form mapping, validated by parse_grants, not by _fill). Stop with
         status=blocked if _fill would reject or strip unknown keys under upstream_mcp and cannot
         be told not to with a one-line change.
      4. Tests in tests/unit/test_upstream_mcp_config.py: one test per §3.2 rule (valid file,
         each refusal, message has the path and not the value), the http host rule (localhost,
         127.0.0.1, single-label name allowed; dotted name refused), the verb/gate pairing and
         effect rules, read_only/destructive derivation is not here (that is p7); for grants:
         `*`, `@domain`, exact email (case-insensitive), union sorted, unverified gives (),
         empty email gives (), GrantStore re-read on mtime change (use os.utime with a new
         mtime_ns), bad re-read keeps last good grants. In tests/unit/test_admin_config.py a test
         that the default config has `upstream_mcp == {}`.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_config.py tests/unit/test_admin_config.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_config.py -q --cov=privacyfence.upstream_mcp_config --cov-branch --cov-report=term-missing reports 100%
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
      Read first: docs/1091/plan.md §2 (verified email), §3.3, §3.5; docs/adr/0272-a-single-user-install-provisions-its-one-person-as-verified.md; docs/adr/0072-org-mode-persists-only-sealed-refresh-tokens.md
      1. principal.py: add `email_verified: bool = False` to Principal, after is_admin, and extend
         the docstring: "email_verified is whether the email was verified at sign-in (organization
         mode); domain grants key on email only when it is true (docs/1091/plan.md §3.3)."
      2. web/single_user_auth.py: SINGLE_USER_PRINCIPAL gets `email_verified=True` (ADR 0272).
      3. web/oauth_provider.py handle_idp_callback: after check_authz_policy, replace principal
         with `dataclasses.replace(principal, email_verified=org_identity.session_email_verified(claims,
         principal, <the AuthzPolicyConfig this provider holds>))`. Stop with status=blocked if the
         provider holds no AuthzPolicyConfig-compatible object for that call.
      4. _mint_tokens adds `"email_verified": principal.email_verified` to the access-token claims;
         _OrgRefreshToken and SealedRefreshRecord gain `email_verified: bool = False`, set at mint
         and carried on rotation; the sealed-record decoder treats a missing key as False (old
         records keep working, unverified). Wherever a Principal is rebuilt from a refresh record,
         pass email_verified through.
      5. web/mcp_auth.py principal_from_access_token: `email_verified=claims.get("email_verified") is True`.
         Leave ScopedTokenVerifier unchanged (p9 adds it).
      6. Tests: principal default False; single-user principal True; an IdP sign-in with
         email_verified true yields a token whose principal has email_verified True, and false
         yields False; a refresh rotation keeps it; a sealed record written without the key
         decodes as False; principal_from_access_token reads only the literal True (the string
         "true" gives False).
    acceptance:
      - python3 -m pytest tests/unit/test_principal.py tests/unit/web/test_oauth_provider.py tests/unit/web/test_mcp_auth.py tests/unit/web/test_sealed_refresh_store.py -q passes
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
      1. Create src/privacyfence/upstream_claims.py with the constants, CallerClaim, ClaimSigner,
         actor_from_context, service_caller and ClaimAuth exactly as §3.5 gives them. ClaimSigner
         uses PyJWT `jwt.encode(payload, key.private_key, algorithm="RS256", headers={"kid":
         key.kid, "typ": CLAIM_TYPE})`, and jwks() returns oidc_provider.jwks(key). Add
         `load_signer(issuer_url: str) -> ClaimSigner` that calls
         oidc_provider.load_or_create_signing_key(org_dir() / KEY_FILENAME) and sets issuer to
         issuer_url.rstrip("/") + "/upstream-mcp".
      2. actor_from_context: scoped_tokens.current_token_scope() first, then
         agent_identity.current_agent(), shapes per §3.5 table; a None agent gives
         `{"type": "ai_client", "attested": False, "name": "", "claimed_name": ""}`.
      3. ClaimAuth(signer, upstream, caller, tool) subclasses httpx2.Auth; auth_flow sets
         `request.headers["Authorization"] = "Bearer " + signer.mint(...)` with a fresh mint per
         request, then yields the request.
      4. Tests in tests/unit/test_upstream_claims.py (key from tmp_path via
         load_or_create_signing_key): decode with jwt.decode against the jwks public key,
         audience=upstream.audience, issuer; header typ and kid; every payload claim of §3.5;
         exp == iat + 60; two mints give different jti; email is "" when not verified; actor for a
         scoped token, an attested agent (name set, claimed_name ""), a claimed CLIENT_INFO agent
         (name "", claimed_name set), no agent; service_caller shape; ClaimAuth sets a different
         bearer on two requests (drive auth_flow with httpx2.Request objects). A claim decoded with
         the wrong audience is rejected (jwt.InvalidAudienceError).
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_claims.py -q passes
      - python3 -m pytest tests/unit/test_upstream_claims.py -q --cov=privacyfence.upstream_claims --cov-branch --cov-report=term-missing reports 100%
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
      1. policy/registry.py: add `def rebuild_registry() -> None` that does
         `TOOL_REGISTRY.clear(); TOOL_REGISTRY.update(_build_registry())`.
      2. policy/propose.py: add `def refresh_all_operations() -> None` that rebinds the module
         global ALL_OPERATIONS with the same expression as its definition (`global ALL_OPERATIONS`).
      3. Create src/privacyfence/upstream_mcp_registry.py with register(upstreams) and
         unregister_all() exactly per §3.4 (collision check before any write; the module-level set
         of added names; gate._TOOL_LAYOUT gets gate.WIDE for review and popup tools).
      4. tests/conftest.py: add an autouse fixture `_restore_upstream_tool_tables` that calls
         upstream_mcp_registry.unregister_all() after each test (no-op when nothing was
         registered). This phase owns conftest.py for the plan.
      5. Tests in tests/unit/test_upstream_mcp_registry.py: after register, each table holds the
         expected values; TOOL_REGISTRY has the entry with the verb; ALL_OPERATIONS contains
         "kh.search"; mcp_dispatch's check_policy no longer raises Unknown tool for kh_search (use
         the existing test_mcp_dispatch helper pattern); scoped_tokens accepts kh_search as a token
         tool (the check at scoped_tokens.py:728); policy.propose offers no rule candidate for
         kh.propose_change (no scope); a collision refuses with nothing written; unregister_all
         restores every table and both derived values exactly. Add one test each to
         tests/unit/policy/test_registry.py (rebuild_registry is in place: the same object) and
         tests/unit/policy/test_propose.py (refresh_all_operations picks up a new operation).
      6. Stop with status=blocked if any module other than the two above derives an import-time
         copy of TOOL_TO_GATE, TOOL_TO_OPERATION, TOOL_TO_VERB or EFFECT_BY_TOOL that the tests in
         step 5 show missing kh_search; name the module.
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
      Read first: docs/1091/plan.md §3.5, §3.9; src/privacyfence/web/routes_mcp.py:600-700 (how the low-level mcp 2.x Server is built); tests/integration/test_mcp_daemon_contract.py
      1. Create scripts/upstream_mcp_reference_server.py: a Starlette app with the mcp 2.x
         low-level Server over StreamableHTTPSessionManager at `/mcp`, built by
         `build_app(*, audience: str, issuer: str | None, jwks_url: str | None, public_key_pem:
         bytes | None, documents: list[dict] | None = None) -> Starlette`. Exactly one of
         jwks_url / public_key_pem is given. Every request needs `Authorization: Bearer <jwt>`;
         verify with PyJWT (RS256 only, aud = audience, iss when given, exp/nbf with 0 leeway,
         header typ == "pf-upstream-claim+jwt"), keep seen jti values until their exp and refuse a
         repeat; any failure answers HTTP 401 with `{"error": "invalid_claim"}`. Pass the decoded
         claims to tool handlers through a contextvar.
      2. Tools: `search` (param `query`, string) returns the documents whose `domain` is in the
         claim's domains and whose title or body contains query, as JSON text; `get_doc` (param
         `id`) returns the document only when its domain is allowed, else the error text "not
         found"; `propose_change` (params `id`, `text`) returns `{"proposal": "accepted", "by":
         <sub>}`. A service claim (act.type == "service") may list tools and gets no documents.
         Default documents: four, in domains general, general, hr, finance.
      3. `main()` with argparse: `--port` (default 8099), `--host` (default 127.0.0.1),
         `--audience` (required), `--issuer`, `--jwks-url`, `--public-key-file`; prints
         `listening on <host>:<port>` and runs uvicorn. The module docstring says it is a reference
         for upstream implementers and for tests, not a production server.
      4. Tests in tests/unit/test_upstream_mcp_reference_server.py drive build_app in process
         with the real mcp client over an httpx2 ASGI transport (the pattern in
         tests/unit/web/test_routes_mcp.py) and claims minted by
         privacyfence.upstream_claims.ClaimSigner with a tmp_path key: search filters by domains;
         get_doc of an hr document without hr is "not found"; missing, expired, wrong-aud,
         wrong-typ and replayed claims get 401.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_reference_server.py -q passes
      - ruff check scripts/upstream_mcp_reference_server.py passes
  - id: p7-client-connector
    title: The upstream MCP client, tool catalog and connector
    depends_on: [p1-tool-input-schema, p5-tool-registration, p6-reference-upstream]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_client.py
      - src/privacyfence/connectors/upstream_mcp.py
      - src/privacyfence/safe_errors.py
      - pyproject.toml
      - requirements/*.lock.txt
      - scripts/pyinstaller_common.py
      - tests/unit/test_upstream_mcp_client.py
      - tests/unit/connectors/test_upstream_mcp_connector.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_systemic_gate_invariants.py
      - tests/unit/web/test_tool_schema_portability.py
      - tests/unit/test_safe_errors.py
    brief: |
      Read first: docs/1091/plan.md §3.6; docs/coding-and-testing-guidelines.md §1.5, §2.6, §3; src/privacyfence/connectors/peek.py (gated_call and _auto_audit patterns); sync/src/privacyfence_sync/client.py:40-80
      1. pyproject.toml: move `httpx2>=2.5` from the test/dev extra to the runtime dependencies,
         with a comment that mcp's client transport needs it for upstream MCP (docs/1091/plan.md
         §3.6). Run scripts/update_dependency_locks.sh and commit the requirements/*.lock.txt it
         changes. Stop with status=blocked if the lock run adds any package not already in
         requirements/runtime.lock.txt.
      2. Create src/privacyfence/upstream_mcp_client.py with UpstreamMcpClient, CallResult,
         UpstreamMcpClientError, UpstreamMcpToolError and UpstreamToolCatalog exactly per §3.6
         (messages verbatim). The caller argument is an upstream_claims.CallerClaim; list_tools
         uses upstream_claims.service_caller().
      3. safe_errors.py: add UpstreamMcpClientError and UpstreamMcpToolError to the
         public_message allowlist, following how an existing connector error type is listed; add
         a test in tests/unit/test_safe_errors.py.
      4. Create src/privacyfence/connectors/upstream_mcp.py with UpstreamMcpConnector per §3.6.
         It gets the CallerClaim from a callable passed to its constructor (`caller: Callable[[],
         CallerClaim] | None = None`), never by reading contextvars itself; p8 passes
         upstream_mcp_runtime.caller_for bound to the upstream. Module docstring states the auto /
         review / popup handling and that no connector code reads the caller (ADR 0036, amended
         by ADR 0279).
      5. scripts/pyinstaller_common.py: add privacyfence.connectors.upstream_mcp to the hidden
         imports list.
      6. Add UpstreamMcpConnector to CONNECTOR_CLASSES in
         tests/unit/connectors/test_readme_manifest_alignment.py and
         tests/unit/test_systemic_gate_invariants.py. Stop with status=blocked if either test then
         requires README or table rows for a connector with no static tools.
      7. Tests: tests/unit/test_upstream_mcp_client.py runs the p6 reference app in process
         (mount it as the httpx2 transport) for list_tools, call_tool text and structured results,
         the 1 MB cap, is_error, an unreachable URL, and the catalog rules (description cleaning
         and cap, approval sentence per gate, reason injected, each skip reason, missing()).
         tests/unit/connectors/test_upstream_mcp_connector.py follows §2.6: TestDispatch, the auto
         tool writes an audit row and never calls the gate, the review tool calls gated_call with
         the result and pii_scan_text, the popup tool calls gated_call before the upstream (assert
         call order) and a deny never reaches the upstream, preview holds only metadata,
         assert_all_tools_leave_an_audit_trail, TestToolDefinitions via
         assert_tool_definitions_complete on a connector with a fake catalog. In
         tests/unit/web/test_tool_schema_portability.py add one case that runs its checks on
         that fake-catalog connector.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_client.py tests/unit/connectors/test_upstream_mcp_connector.py tests/unit/test_safe_errors.py -q passes
      - python3 -m pytest tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_systemic_gate_invariants.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_pyinstaller_hidden_imports.py -q passes
      - grep -n "httpx2" requirements/runtime.lock.txt matches
  - id: p8-org-wiring
    title: Wire upstreams into the organization server, JWKS route and status
    depends_on: [p3-verified-email, p7-client-connector]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_runtime.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/web/server.py
      - src/privacyfence/web/routes_upstream_mcp.py
      - src/privacyfence/web/mcp_dispatch.py
      - tests/unit/test_upstream_mcp_runtime.py
      - tests/unit/web/test_routes_upstream_mcp.py
      - tests/unit/test_daemon_main.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/integration/test_upstream_mcp_org.py
    brief: |
      Read first: docs/1091/plan.md §3.5, §3.7 (first two bullets), §3.8; src/privacyfence/daemon_main.py:960-1010, :1495-1515, :1649-1700; src/privacyfence/web/server.py:1360-1390; src/privacyfence/web/routes_oidc.py
      1. Create src/privacyfence/upstream_mcp_runtime.py with the functions of §3.8.
         caller_for(upstream): principal = current_principal(); scope = current_token_scope();
         without a scope, domains = grants.domains_for(upstream.id, principal.email,
         principal.email_verified); with a scope, domains = sorted(set(grants.domains_for(upstream.id,
         scope.principal_email, getattr(scope, "principal_email_verified", False))) &
         set(getattr(scope, "upstream_domains", {}).get(upstream.id, ()))) (p9 adds the two
         fields; getattr keeps this phase independent); act = upstream_claims.actor_from_context().
         Return CallerClaim per §3.5.
      2. web/routes_upstream_mcp.py: `build_routes(signer) -> list[Route]` with GET
         `/upstream-mcp/jwks` returning signer.jwks() as JSON with `Cache-Control: max-age=300`.
         Mount it in web/server.py's organization app beside routes_oidc (server.py:1383) only
         when upstream_mcp_runtime.configured().
      3. daemon_main.py: the local-mode refusal (message in §3.8) beside the OIDC one at :974;
         in organization mode with the variable set, load and register per §3.8 before the web
         server is built, `upstream_claims.load_signer(server_config.issuer_url)`, a GrantStore on
         admin_config.admin_config_path(), configure(); in build_connectors append
         `upstream_mcp_runtime.connectors_for_principal()` when configured(); pass
         `upstream_mcp_runtime.lifespan(dispatcher.notify_tools_changed)` into the lifespans list
         the org web server gets.
      4. web/mcp_dispatch.py status(): add `"upstreams": upstream_mcp_runtime.status()` only when
         configured() (the key is absent otherwise, so existing status tests are unchanged).
      5. Tests: runtime unit tests (caller_for for a verified person, an unverified person, a
         scoped token with and without upstream_domains using a stand-in object; status states;
         lifespan backoff with a fake client that fails twice then succeeds, then
         notify_tools_changed called once); the JWKS route; daemon refusal outside org mode;
         status key present/absent. tests/integration/test_upstream_mcp_org.py: an organization
         daemon on loopback (the test_mcp_daemon_contract.py pattern) with the p6 reference server
         on another loopback port verifying via the daemon's /upstream-mcp/jwks; an MCP client
         lists `ref_search`, calls it as a person with grants {general} and gets only general
         documents; a popup tool with the gate faked to deny never reaches the reference server.
         Stop with status=blocked if the org daemon cannot be started in-test the way the contract
         test starts one; say what is missing.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_runtime.py tests/unit/web/test_routes_upstream_mcp.py tests/unit/test_daemon_main.py tests/unit/web/test_mcp_dispatch.py -q passes
      - python3 -m pytest tests/integration/test_upstream_mcp_org.py tests/integration/test_mcp_daemon_contract.py -q passes
  - id: p9-scoped-token-domains
    title: Scoped tokens carry per-upstream domains, set in Settings
    depends_on: [p8-org-wiring]
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
      1. scoped_tokens.py: add the two ScopedToken fields of §3.7, serialized only when
         non-default exactly like allow_personal_data_reads (scoped_tokens.py:219, :246, :310);
         the create path validates upstream_domains keys against configured upstream ids
         (upstream_mcp_runtime) and values against the domain pattern of §3.3, and refuses a
         domain the creator does not currently hold for that upstream with
         `"You can only give a token domains you have: <domain>"`.
      2. web/mcp_auth.py ScopedTokenVerifier: add `"email_verified": found.principal_email_verified`
         to the claims.
      3. web/routes_settings.py: the create-token request accepts `upstream_domains` (object of
         id -> list of strings), normalised to sorted lists before the step-up fingerprint is
         computed, and sets principal_email_verified from the browser session's verified bit.
         Stop with status=blocked if the session exposes no verified bit.
      4. web/server.py: the Settings payload that lists token tools (server.py:387) adds
         `upstreams: [{"id", "label", "tools": [...exposed names], "domains": [...the person's
         current domains]}]` when configured.
      5. settings_window_html.py: in the New token form, for each upstream whose tools are
         selected, a fieldset "Data domains for <label>" with one checkbox per domain, none checked;
         the hint "The token sees only the domains you tick, and never more than you can see
         yourself." Unticked means none.
      6. scoped_token_proposals.py: refuse a proposal naming any upstream tool with the §3.7 message.
      7. Tests: store round-trip with and without the fields (old files load); verifier claim;
         create route normalisation, the fingerprint changes when domains change, refusal of an
         unheld domain; payload; form markup; proposal refusal; the browser test ticks one
         domain and the created token holds exactly it.
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_scoped_token_proposals.py tests/unit/web/test_mcp_auth.py tests/unit/web/test_routes_settings.py tests/unit/web/test_server.py tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -q passes (not skipped)
  - id: p10-docs-and-adrs
    title: Operator and implementer docs, ADRs 0278-0280, changelog
    depends_on: [p9-scoped-token-domains]
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
      Read first: docs/1091/plan.md (all, especially §3 and §4); docs/adr/README.md (template and index format); CONTRIBUTING.md "Decisions, plans and ADRs"
      1. docs/upstream-mcp.md: title and 3-line summary; for operators: the file format (§3.2
         example and every rule), the grants (§3.3), what a person and a scoped token see
         (§3.7), privacyfence_status, that changes to the file need a restart and grant edits do
         not; for upstream implementers: verify RS256 against /upstream-mcp/jwks, require typ,
         aud, iss, exp, keep a jti cache, which claims may change results (sub, domains, act.type)
         and which are audit-only, the service actor, a Postgres row-level-security sketch keyed
         on domains (`SET LOCAL app.domains = ...` plus a policy `domain = ANY(...)`), and how to
         run scripts/upstream_mcp_reference_server.py; a "Declaring a writing tool with a read
         verb bypasses the approval card" warning.
      2. ADRs 0278, 0279, 0280 per §4, Status Accepted with today's date, each with Context,
         Decision, Alternatives considered (from §3's "Rejected" lines), Consequences, Verification
         (the tests of p2-p9). 0279's Status says it amends ADR 0036; 0280's says it amends 0164 and
         0236. Add their rows to docs/adr/README.md in the index format.
      3. docs/configuration-reference.md: PRIVACYFENCE_UPSTREAM_MCP_FILE and the admin_config
         upstream_mcp key, linking docs/upstream-mcp.md. docs/org-mode-setup-guide.md: one
         paragraph linking it. docs/testing-policy.md, layer 5: one line that a connector with no
         external provider (the upstream MCP connector) has no live fixture and is covered by the
         reference server (ADR 0278). docs/README.md: a row for docs/upstream-mcp.md in its
         existing format.
      4. CHANGELOG.md under "## [Unreleased]": an "Added" entry for upstream MCP servers behind
         PrivacyFence with signed caller claims and domain grants, and one for scoped-token data
         domains. No version heading.
    acceptance:
      - ls docs/adr/0278-*.md docs/adr/0279-*.md docs/adr/0280-*.md succeeds
      - grep -c "027[89]\|0280" docs/adr/README.md prints 3 or more
      - grep -n "PRIVACYFENCE_UPSTREAM_MCP_FILE" docs/configuration-reference.md docs/upstream-mcp.md matches
      - python3 -m pytest tests/unit -q -k "docs or adr or changelog" passes
```
