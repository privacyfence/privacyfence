# Plan: OAuth-authenticated hosted upstream MCP servers (issue 1155)

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator declares a hosted MCP server in the upstream file with `auth: oauth` (for example Canva at `https://mcp.canva.com/mcp` or Framer at `https://mcp.framer.com/mcp`), with no `audience` and no identity claim, and restarts. Each person then clicks **Connect** for that server on `/connect`, signs in at the vendor and comes back; the tools appear to their AI client as `<id>_<tool>` with the same gates, approval cards, scoped-token rules and audit log as any upstream. Before anyone has connected, `privacyfence_status` says `waiting_for_connection`; a person who has not connected has none of the server's tools.

**Scope:** One opt-in per upstream (`auth: oauth`, default `claim` as today). New: discovery of the server's OAuth metadata, a client registration (declared `client_id`, else dynamic registration), a per-person token file, a Connect-page row and two routes, a bearer on the upstream request, and an OAuth mode for tests. The tool list is fetched once, with the first connected person's token, and pinned. Not changed: claim upstreams, group maps and audiences, the policy tables. Not built (owner's decisions): a Disconnect button, and the Client ID Metadata Document method.

**Security:** An OAuth upstream never receives PrivacyFence's signed identity claim, and the bearer is sent only to the declared server's own origin. Metadata-advertised endpoints must be https, on the issuer's host, and not private IP literals (3.3). Tokens are plaintext JSON at mode 0600 per person, like the other third-party connectors. A scoped token acts through its creator's connection, as built-in connectors do (owner's decision). `allowed_principal_types` is now enforced in every auth mode, which also changes `identity_claim: off` (3.6).

**Review focus:** the claim never leaving for an OAuth upstream and the principal-type check now running in every mode (3.6, ADR 0327); the endpoint host-binding and address rules (3.3, ADR 0328); the tool list taken from the first connected person and pinned (3.7, ADR 0329); scoped tokens using their creator's connection (3.8); the declaration digest for OAuth upstreams (3.1).

## 1. Goal

An organization wants its people to use a hosted third-party MCP server that authenticates each person with the vendor's own OAuth (the MCP authorization spec), such as Canva or Framer, through PrivacyFence, so that cards, scoped tokens and the audit log apply to those tools too. Today an upstream can only be authenticated by PrivacyFence's signed claim (ADR 0285, 0297), which a hosted vendor cannot verify. Issue: https://github.com/privacyfence/privacyfence/issues/1155.

Owner decisions (board ask `a-7dfd02cfe96489be`, answered 2026-11-18): client registration is **declared `client_id`, else RFC 7591 dynamic registration** (the metadata-document method is left out); a scoped token **may call through its creator's connection**; **no Disconnect button**. A research report (`r-20261118-1`, 2026-11-18) found that both vendors publish RFC 9728 metadata and a registration endpoint (3.9); it did not settle redirect-URI acceptance, which is why 5 has a manual check.

## 2. Current state

- An upstream is parsed by `upstream_mcp_config._parse_one` (`src/privacyfence/upstream_mcp_config.py:236`); the keys are `_UPSTREAM_KEYS` (`:68`), the dataclass is `UpstreamServer` (`:98`), `declaration_digest` (`:113`) pins what an ADR 0300 approval depends on, `_url_ok` (`:156`) is the address check. `audience` is required today (`:268`).
- `UpstreamMcpClient._run` (`src/privacyfence/upstream_mcp_client.py:81`) opens one Streamable HTTP session per call and authenticates only with `IdentityClaimAuth` when `identity_claim == "required"`; any failure becomes `unreachable` or `claim_failed`. `call_tool` is at `:119`, `list_tools` at `:112`.
- `UpstreamMcpConnector._forward` (`src/privacyfence/connectors/upstream_mcp.py:581`) asks the runtime-supplied `caller` callable only when the claim is required, and `CallerNotAllowedError` becomes `UpstreamCallRefusedError`. `allowed_principal_types` is therefore enforced only inside `upstream_mcp_runtime.caller_for` (`src/privacyfence/upstream_mcp_runtime.py:176-214`), and an upstream with `identity_claim: off` skips it (`tests/unit/connectors/test_upstream_mcp_connector.py:303` pins that).
- `upstream_mcp_runtime` fetches each tool list once at startup with the claim signer (`_fetch_tool_list`, `:296`), builds one connector per upstream for every person (`connectors_for_principal`, `:114`), and reports `status()` (`:227`) and `admin_rows()` (`:255`). Audience helpers: `audiences_for_email` (`:126`), `held_audiences` (`:217`), `all_group_audiences` (`:138`).
- Per-person OAuth connectors: `/oauth/start/{service}` and `/oauth/callback/{service}` in `src/privacyfence/web/routes_connect.py:486,528` with an in-memory single-use `_PendingAuthStore` (TTL 600 s, `:206`), the ADR 0178 session check, and `connector_registry.evict` on success. Tokens are plaintext 0600 JSON written with `secure_files.atomic_write_json` (`src/privacyfence/secure_files.py:193`) under `paths.user_dir(principal)/credentials/` (`src/privacyfence/paths.py:149`). A connector without a token is absent for that person. `org_identity.generate_pkce_pair()` (`src/privacyfence/org_identity.py:222`) makes the S256 pair. No disconnect exists for any connector. The Connect page lists services in `_render_connect_page` (`:1095`) and flashes with `_flash_html` (`:879`).
- `routes_upstream_mcp.build_routes(signer)` mounts `/upstream-mcp/jwks` (`src/privacyfence/web/server.py:728-734`), and `routes_connect.build_routes` is mounted at `:750`.
- The installed `mcp` SDK (2.3.0, pinned `mcp>=2.2,<3.0.0` in `pyproject.toml:98`) has pure helpers in `mcp.client.auth.utils` (`build_protected_resource_metadata_discovery_urls` `:64`, `build_oauth_authorization_server_metadata_discovery_urls` `:159`, `extract_resource_metadata_from_www_auth` `:52`, `validate_metadata_issuer` `:263`), models in `mcp.shared.auth` (`OAuthToken`, `OAuthMetadata`, `ProtectedResourceMetadata`, `OAuthClientInformationFull`) and `mcp.shared.auth_utils` (`resource_url_from_server_url` `:9`, `check_resource_allowed` `:31`, `calculate_token_expiry` `:68`). Its `OAuthClientProvider` blocks on a browser callback inside a request and does not persist server metadata, so it is not used (3.2).
- ADR 0297 (`docs/adr/0297-…`, line 23) states `Authorization` is never set or read by PrivacyFence; `docs/identity-claim.md:21` and `docs/upstream-mcp.md:309` repeat it.
- Test patterns: `tests/unit/test_upstream_mcp_config.py`, `tests/unit/test_upstream_mcp_client.py`, `tests/unit/connectors/test_upstream_mcp_connector.py`, `tests/unit/test_upstream_mcp_runtime.py` (`configured` fixture, `_FlakyClient`, `TestLifespan`), `tests/unit/web/test_routes_connect.py` (`TestMiroOAuth` `:1343`, helpers `_app`, `_client`, `_signed_in` `:54-69`), `tests/unit/test_miro_auth.py`, `tests/unit/test_systemic_gate_invariants.py` (`TOKEN_WRITE_SITES` `:184`), `tests/integration/test_upstream_mcp_org.py` (`stack` fixture `:91`), `scripts/upstream_mcp_reference_server.py` (`build_app` `:146`).

## 3. Design

### 3.1 Operator file keys

New optional per-upstream keys (errors read `<source>: upstreams[<i>].<key>: <problem>`):

| Key | Rule | Problem text |
|---|---|---|
| `auth` | `claim` (default) or `oauth` | `must be claim or oauth` |
| `oauth` | an object, only with `auth: oauth`; every key inside is optional | `needs auth oauth` (with `claim`); `must be an object` |
| `oauth.scopes` | list of 1-20 strings, each 1-100 characters, no whitespace or control character | `must be a list of 1-20 scopes, each 1-100 characters with no whitespace` |
| `oauth.client_id` | string, 1-200 characters, no whitespace or control character | `must be 1-200 characters with no whitespace` |
| `oauth.client_secret_env` | name of an environment variable, `[A-Z][A-Z0-9_]{0,63}`, only with `client_id` | `must be an environment variable name in capital letters, digits and underscores`; `needs client_id` |
| `oauth.issuer` | an `https://` address with no user name, query or fragment (a path is allowed; `http://` only for a loopback host) | `must be an https:// address with no user name, query or fragment` |

An unknown key under `oauth` is `upstreams[<i>].oauth.<key>: is not a known key`. With `auth: oauth` these keys are refused with `is not used with auth oauth`: `audience`, `identity_claim`, `claim_lifetime_seconds`, `email_for_visibility`, `audiences_tool`, `audiences_refresh_seconds`; `audience` is no longer required. The parsed `UpstreamServer` of an OAuth upstream has `audience=""`, `identity_claim="off"`, `claim_lifetime_seconds=60`, `email_for_visibility=False`, `audiences_tool=None`. `allowed_principal_types` is still accepted. For `auth: oauth` the `url` must be `https://`, or `http://` only for `localhost` or a loopback address (the single-word host names `_url_ok` allows are refused): `must be an https:// address (http:// only for localhost or a loopback address) when auth is oauth`.

New in `upstream_mcp_config.py`: `UpstreamOAuthSettings(scopes: tuple[str, ...] = (), client_id: str | None = None, client_secret_env: str | None = None, issuer: str | None = None)` (frozen); `UpstreamServer.auth: str = "claim"`, `UpstreamServer.oauth: UpstreamOAuthSettings | None = None`, and the property `UpstreamServer.sends_claim` = `auth == "claim" and identity_claim == "required"` (the one predicate every claim site uses). `_url_ok` stays as it is.

**Digest.** For `auth: claim` `declaration_digest` is byte-for-byte what it is today (a test pins a hard-coded value computed before the change), so existing ADR 0300 approvals stay valid. For `auth: oauth` the pinned object is `{"auth": "oauth", "client_id": <declared or "">, "effect", "gate", "issuer": <pin or "">, "operation", "scopes": <sorted list>, "url", "verb"}`, canonicalised and hashed the same way. A client id that dynamic registration produced is never part of it (it is unknown at startup).

### 3.2 Module layout

- `src/privacyfence/upstream_oauth_tokens.py` (new, p2): `UpstreamOAuthError`, `OAuthClient`, `TokenRecord`, token file functions, `build_authorize_url`, `exchange_code`, `PendingUpstreamAuthStore`, `UpstreamTokenProvider`, `BearerAuth`, `origin_of`.
- `src/privacyfence/upstream_oauth_discovery.py` (new, p3): `OAuthEndpoints`, `discover`, `RegistrationStore`.
- No module-level mutable state in either: locks and stores are instance attributes created by the runtime and the routes, so nothing needs a reset in `tests/conftest.py`.
- Network I/O in both modules uses `httpx2.AsyncClient(transport=<injected or None>, timeout=httpx2.Timeout(10.0))` with redirects off (the httpx2 default), reads at most 64 KiB of any body, and never puts a response body, token, code or secret into an exception, a log line or a message.
- Rejected: the SDK's `OAuthClientProvider`. It starts an interactive authorization from inside a request, and it keeps no authorization-server metadata, so a refresh after a restart would go to `<origin>/token`, wrong whenever the authorization server is on another host. The pure helpers are used instead, and a unit test imports every helper name this plan uses so an SDK bump within `mcp<3` that removes one fails loudly.

### 3.3 Discovery (`upstream_oauth_discovery.discover`)

`async def discover(upstream: UpstreamServer, *, transport: httpx2.AsyncBaseTransport | None = None) -> OAuthEndpoints`; `OAuthEndpoints` is a frozen dataclass: `issuer: str`, `authorization_endpoint: str`, `token_endpoint: str`, `registration_endpoint: str | None`, `resource: str | None`, `scope: str | None`, `token_auth_methods: tuple[str, ...]`, `loopback: bool`.

1. `loopback` is true when `upstream.url` is `http://` (the config already limits that to a loopback host). Every URL checked below goes through `_checked_url(url, issuer_host, loopback)`: it must be `https` (or `http` when `loopback`), have no user name or fragment, and, unless `loopback`, its host must not be an IP literal in a private, loopback, link-local, multicast, reserved or unspecified range, must have at least two labels and must not end in `.local`, `.internal`, `.home.arpa` or `.lan`. Each failure raises `UpstreamOAuthError("invalid_metadata")`.
2. Send one unauthenticated `POST` of `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"PrivacyFence","version":"0"}}}` to `upstream.url` with `Accept: application/json, text/event-stream`. A transport error or timeout raises `UpstreamOAuthError("unreachable")`. On a 401 read `resource_metadata` from `WWW-Authenticate` with `extract_resource_metadata_from_www_auth`; otherwise there is no hint.
3. Candidate metadata URLs come from `build_protected_resource_metadata_discovery_urls(hint, upstream.url)`. Fetch them in order with `GET` (each URL passes `_checked_url` against the upstream's own host) until one returns 200 with a body that `ProtectedResourceMetadata.model_validate_json` accepts. None: `UpstreamOAuthError("invalid_metadata")`.
4. Check the resource: `check_resource_allowed(resource_url_from_server_url(upstream.url), str(prm.resource))` when `prm.resource` is set, else refuse. `resource` in the result is `str(prm.resource)`.
5. The authorization server is `upstream.oauth.issuer` when pinned (it must be one of `prm.authorization_servers`, else refuse), otherwise the first of `prm.authorization_servers`; with none listed refuse. It must pass `_checked_url` (host rules, https). Its host is `issuer_host`.
6. Fetch `build_oauth_authorization_server_metadata_discovery_urls(issuer, upstream.url)` in order until one parses as `OAuthMetadata`; call `validate_metadata_issuer(metadata, issuer)`. None: refuse.
7. Require `authorization_endpoint` and `token_endpoint`; each, and `registration_endpoint` when present, must pass `_checked_url` and have exactly the issuer's host and port. Require `"S256"` in `code_challenge_methods_supported` (an absent list is a refusal). When `registration_endpoint` is absent and `upstream.oauth.client_id` is not declared, `discover` still returns (the refusal is made by `RegistrationStore`, 3.4).
8. Scope sent to the authorization server: `" ".join(upstream.oauth.scopes)` when declared, else `" ".join(prm.scopes_supported)` when that list is non-empty, else `None`.
9. `token_auth_methods` is `tuple(metadata.token_endpoint_auth_methods_supported or ("client_secret_basic",))`.
10. Never used: the SDK fallbacks to `<origin>/register`, `<origin>/token` and the legacy origin well-known URL.

### 3.4 Client registration (`RegistrationStore`)

`RegistrationStore(directory: Path, *, transport=None)`; the runtime passes `paths.org_dir() / "upstream_oauth"`. It holds one `asyncio.Lock` per upstream id (single flight).

- `async ensure_client(upstream, endpoints, redirect_uri) -> OAuthClient` returns, in order: (a) with `upstream.oauth.client_id` declared: that id, with the secret read from the environment variable `client_secret_env` at call time (a declared name whose variable is empty raises `UpstreamOAuthError("not_configured")`); `auth_method` is `none` without a secret, else `client_secret_post` when `endpoints.token_auth_methods` lists it, else `client_secret_basic`; nothing is written. (b) A cached registration from `<directory>/<id>.json` whose `issuer` and `redirect_uri` equal the current ones and whose `client_secret_expires_at` is absent, 0 or more than 60 seconds ahead. (c) A new registration: `endpoints.registration_endpoint` absent raises `UpstreamOAuthError("registration_failed")`; otherwise `POST` JSON `{"client_name": "PrivacyFence", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": <m>, "scope": <endpoints.scope, omitted when None>}` where `<m>` is `none` if `endpoints.token_auth_methods` lists it, else `client_secret_post` if listed, else `client_secret_basic`. Parse the answer as `OAuthClientInformationFull`; require a `client_id`, and when it lists `redirect_uris` require ours among them; any failure raises `UpstreamOAuthError("registration_failed")` without the body. Write `{"version": 1, "upstream_id", "issuer", "redirect_uri", "client_id", "client_secret", "client_secret_expires_at", "auth_method", "registered_at"}` with `secure_files.atomic_write_json` (directory made with `secure_files.secure_mkdir`).
- `client_for(upstream_id: str) -> OAuthClient` (no network) returns the declared client (secret from the environment) or the cached registration, else raises `UpstreamOAuthError("not_configured")`. The token provider uses it for refreshes.
- `forget(upstream_id)` removes the cache file; the provider calls it through the runtime when the token endpoint answers `invalid_client`.
- The redirect URI is `<issuer_url without trailing slash>/upstream-mcp/<id>/callback`; a changed server address therefore re-registers on the next Connect. There is no relay: the redirect goes straight to the server (ADR 0259's relay serves only built-in apps).

### 3.5 Tokens (`upstream_oauth_tokens`)

- `TokenRecord` (frozen): `upstream_id`, `access_token`, `refresh_token: str | None`, `expires_at: float | None`, `scope: str`, `issuer`, `client_id`, `auth_method`, `token_endpoint`, `resource: str | None`, `connected_at: float`. The file is `paths.user_dir(principal) / "credentials" / "upstream_mcp" / "<id>.json"` (`token_path(principal, upstream_id)`), `{"version": 1, ...}`, written with `secure_files.atomic_write_json` after `secure_mkdir`, 0600, plaintext like every other third-party token (ADR 0072 rejects encryption under a key the server holds). `token_paths(upstream_id)` lists `paths.data_dir() / "users" / "*" / "credentials" / "upstream_mcp" / "<id>.json"`, newest modification first. `load_token(path)` returns `None` when the file is absent or malformed.
- `build_authorize_url(authorization_endpoint, client_id, redirect_uri, state, code_challenge, scope, resource) -> str`: query `response_type=code`, `client_id`, `redirect_uri`, `state`, `code_challenge`, `code_challenge_method=S256`, plus `scope` and `resource` when not `None`.
- `async exchange_code(*, token_endpoint, client, code, redirect_uri, code_verifier, resource, upstream_id, issuer, transport=None) -> TokenRecord`: form `POST` with `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `code_verifier` and `resource` when set; client authentication by `client.auth_method` (`client_secret_post`: `client_secret` in the form; `client_secret_basic`: `httpx2.BasicAuth(quote(id, safe=""), quote(secret, safe=""))`; `none`: nothing). The answer must parse as `OAuthToken` with `token_type` equal to `bearer` ignoring case and a non-empty `access_token`; `expires_at = calculate_token_expiry(expires_in)` (a missing `expires_in` means `None`: no proactive refresh). Failure raises `UpstreamOAuthError("auth_expired")` for a 400 or 401 and `UpstreamOAuthError("unreachable")` otherwise, never quoting the body.
- `PendingUpstreamAuthStore(ttl_seconds=600, max_entries=1000, clock=time.time)`: `put(state, PendingUpstreamAuth)` and a single-use `pop(state)` that returns `None` after the TTL; `PendingUpstreamAuth` holds `principal_id`, `upstream_id`, `code_verifier`, `redirect_uri`, `issuer`, `client: OAuthClient`, `token_endpoint`, `resource`. A restart drops every pending attempt (the person clicks Connect again).
- `UpstreamTokenProvider(client_source: Callable[[str], OAuthClient], *, clock=time.time, transport=None)`; `async bearer(path: Path, force_refresh: bool = False) -> str`: load the file (absent: `UpstreamOAuthError("not_connected")`); when `force_refresh` or `expires_at` is within 60 seconds, take the per-path `asyncio.Lock`, re-read the file (another call may have refreshed), and if still stale and there is a `refresh_token`, `POST` `grant_type=refresh_token`, `refresh_token`, `client_id` and `resource` (when set) to the stored `token_endpoint` with the client from `client_source(record.upstream_id)`; a returned `client_id` that differs from the record's raises `auth_expired` after deleting the file; the new record keeps the old refresh token when none is returned and is saved. A 400 or 401 answer (`invalid_grant`, `invalid_client`) deletes the file and raises `UpstreamOAuthError("auth_expired")`; any other failure raises `unreachable` and keeps the file. A stale token with no refresh token raises `auth_expired` after deleting the file.
- `BearerAuth(token: str, origin: str)` (an `httpx2.Auth`): sets `Authorization: Bearer <token>` only on a request whose origin (`scheme://host[:port]`, from `origin_of`) equals `origin`; after each response records `status` (the last status code) and `insufficient_scope` (true on a 403 whose `WWW-Authenticate` has `error="insufficient_scope"` or `error=insufficient_scope`).
- Log lines in these modules name the upstream id and an error kind only.

### 3.6 Client, connector and caller policy

- `UpstreamMcpClient` (p4): `list_tools(self, *, bearer: BearerSource | None = None)` and `call_tool(self, tool, arguments, caller, *, bearer: BearerSource | None = None)`, where `BearerSource = Callable[[bool], Awaitable[str]]` (the argument is `force_refresh`). For `upstream.auth == "oauth"` a missing `bearer` raises `UpstreamMcpClientError("not_connected")`; the claim code runs only when `upstream.sends_claim`. `_run` gets the token with `await bearer(False)` (an `UpstreamOAuthError` becomes the client error of the same kind: `not_connected`, `auth_expired`, `unreachable`), sends it through `BearerAuth(token, origin_of(upstream.url))`, and tracks a phase flag. A 401 seen during `session.initialize()` gets exactly one retry with `await bearer(True)` on a fresh session; a 401 after `initialize` (during the call) raises `auth_expired` and is never re-sent; a 403 with `insufficient_scope` raises `insufficient_scope`; everything else stays `unreachable`. Kinds are added to the docstring list. **The claim header is never sent to an OAuth upstream** (a test inspects every request of `initialize`, the call and the session close).
- `UpstreamMcpConnector` (p4) gains two optional constructor arguments: `admit: Callable[[], None] | None` and `bearer: Callable[[], BearerSource] | None`. `_forward` first calls `admit()` when given (any `CallerNotAllowedError` becomes `UpstreamCallRefusedError`, text unchanged), then, for an OAuth upstream, `bearer()` and passes the source to the client; the claim path (`caller`) runs only when `upstream.sends_claim`. Messages (label is `upstream.label`):
  - `not_connected`: `<label> is not connected. Connect <label> on the Connect page (/connect).`
  - `auth_expired`: `The <label> sign-in has expired or was revoked. Connect <label> on the Connect page (/connect) to sign in again.`
  - `insufficient_scope`: `<label> needs more access than PrivacyFence was granted. Connect <label> again on the Connect page (/connect).`
  They are `UpstreamUnavailableError`, and the audit `upstream_status` is the kind. `_forward_audit` metadata gains `"auth": upstream.auth`.
- **The principal-type check runs in every auth mode (p5, ADR 0327).** `upstream_mcp_runtime.admit_caller(upstream)` raises `CallerNotAllowedError(principal_type)` when the current caller's type (`n8n_workflow` when `current_token_scope()` is set, else `claude_client`) is not in `upstream.allowed_principal_types`; `caller_for` calls it instead of its own inline check, and the runtime passes `functools.partial(admit_caller, upstream)` as `admit` for every upstream. This changes `identity_claim: off`: it used to skip the check (`docs/upstream-mcp.md:83` already says "who may call it"). The unit test at `tests/unit/connectors/test_upstream_mcp_connector.py:303` is updated to the new behaviour, not loosened.

### 3.7 Runtime, tool list and status (p5)

- `_Entry` gains `tokens: UpstreamTokenProvider | None`, `registrations: RegistrationStore | None`, `connected_event: asyncio.Event | None` and `candidates_seen: bool`. `configure` builds them for OAuth upstreams only (`RegistrationStore(paths.org_dir() / "upstream_oauth", transport=transport)` and `UpstreamTokenProvider(client_source=lambda id: registrations.client_for(id), transport=transport)`) and raises `org_mode.ConfigurationError` `Upstream "<id>": oauth.client_secret_env names <NAME>, which is not set.` when a declared secret variable is empty at start. It logs the existing "groups ignored" warning variant `admin_config.json: upstream_mcp.<id>.groups is ignored: <id> uses OAuth` for an OAuth upstream with groups.
- **Who sees the tools.** `connectors_for_principal()` returns a connector for an OAuth upstream only when the current principal has a token file (`upstream_oauth_tokens.load_token(token_path(current_principal(), id)) is not None`); it runs inside the connector registry's `principal_scope`, and the connect callback evicts that registry entry (3.8), so a new connection shows its tools on the next request.
- **Tool list.** For an OAuth upstream the lifespan runs `_list_with_a_connection(entry, notify)` instead of `_fetch_tool_list`: loop { `paths = token_paths(id)` (newest first); set `candidates_seen = bool(paths)`; for each path try `entry.client.list_tools(bearer=functools.partial(entry.tokens.bearer, path))`, skipping a candidate whose attempt raises (an `auth_expired` has already deleted that file); on the first success pin the list exactly like `_fetch_tool_list` (`catalog.update`, `listed=True`, the same warnings and `notify_tools_changed`), write one audit entry (decision `upstream_tools_listed`, connector the upstream id, summary `Listed <id> tools with a stored connection`, no person named), log the names the upstream listed that are not declared at info level (each name with control characters removed and cut to 48 characters), and return; otherwise wait `asyncio.wait_for(entry.connected_event.wait(), 300)` (a timeout just loops) and clear the event }. `on_connected(upstream_id)` sets the event; the routes call it after saving a token. The list is never refreshed afterwards (ADR 0285's rug-pull rule).
- `bearer_for_current(entry)` returns `functools.partial(entry.tokens.bearer, token_path(current_principal(), entry.upstream.id))`; `has_connection(upstream_id)` is the token-file test for the current principal; `oauth_upstream(upstream_id) -> UpstreamServer | None` returns a declared OAuth upstream.
- `status()` rows gain `"auth"`; for an OAuth upstream `state` is `waiting_for_connection` when not listed and no token file exists anywhere (`candidates_seen` false), `waiting_for_tool_list` when not listed but a candidate exists, else `ready`; they also carry `"connected"` (the caller has a token file) and, when not connected, `"connect_hint": "Connect <label> on the Connect page (/connect)."`, and `"audiences_source": "none"`. `admin_rows()` rows gain `"auth"` and, for OAuth, `"oauth": {"issuer_host": <host of the pinned issuer or None>, "registration": "declared" | "dynamic", "scopes": [...], "connected_people": <count of token_paths(id)>}` and `"audiences_source": "none"`; they never name a person, a token or a client id.
- `held_audiences`, `all_group_audiences` and `audiences_for_email` return nothing for an OAuth upstream (it has no audiences).

### 3.8 Web: Connect page and routes (p6)

New `web/routes_upstream_oauth.py`, `build_routes(*, sessions, connector_registry, issuer_url, transport=None) -> list[Route]`, mounted in `web/server.py` next to `routes_connect` (only when `connector_registry` exists and `upstream_mcp_runtime.configured()`):

- `GET /upstream-mcp/{id}/connect`: no session: 302 to `/login?next=/connect`. An id that is not a declared OAuth upstream: 404 `Not found`. Otherwise `discover` → `RegistrationStore.ensure_client(upstream, endpoints, redirect_uri)` → `generate_pkce_pair()` and `state = secrets.token_urlsafe(32)` → `pending.put(...)` → 302 to `build_authorize_url(...)` with `Cache-Control: no-store`. Any `UpstreamOAuthError` or other failure: log `Upstream <id> connect failed: <kind>` and 302 to `/connect?upstream_error=<id>`.
- `GET /upstream-mcp/{id}/callback`: `pending.pop(state)`; missing, expired or for another upstream id: 400 `Invalid or expired sign-in attempt`. The signed-in principal must equal the pending one (ADR 0178), else log and 302 to `/connect?upstream_error=<id>`. A provider `error` (log only its code, kept to `[A-Za-z0-9_.-]` and 40 characters) or a missing `code` is the same redirect. An `iss` parameter that is present must equal the pending issuer. Then `exchange_code(...)`, `save_token(token_path(principal, id), record)`, `connector_registry.evict(principal.id)`, `upstream_mcp_runtime.on_connected(id)` and 302 to `/connect?upstream_connected=<id>`. The state, code and any body are never logged.
- `routes_connect` calls `routes_upstream_oauth.connect_section_html(principal, flash_connected_id, flash_error_id) -> tuple[str, str]` (flash HTML, rows HTML); `connect_page` reads the two query parameters `upstream_connected` and `upstream_error`, ignores a value that is not a declared OAuth upstream id, and `_render_connect_page` puts the flash after `_flash_html` and the rows after `other_rows`. Each row is built like `_service_row_html`: the label (HTML-escaped), the `Connected` badge or `Not connected`, and a `Connect` or `Reconnect` link to `/upstream-mcp/<id>/connect`. Flash texts: `<label> connected.` and `Could not connect <label> -- sign-in was declined, or the server could not be reached.`
- `web/server.py` token page (`:333-343`): each upstream entry gains `"auth": row["auth"]`.
- Scoped tokens (p7): for an OAuth upstream the New token form shows, instead of the audiences fieldset, the legend `Connection for <label>` and the hint `This token acts through your own <label> connection: it can do what your account there can do.`; the Admin card shows one row `Sign-in` and no group card (3.7's `audiences_source: "none"`).

### 3.9 Facts from outside research (report `r-20261118-1`, 2026-11-18; web text, medium confidence, read through a summarizing fetch tool)

- Canva: protected-resource metadata at `https://mcp.canva.com/.well-known/oauth-protected-resource` names `https://mcp.canva.com` as authorization server and lists 16 scopes; authorization-server metadata at `https://mcp.canva.com/.well-known/oauth-authorization-server` has `registration_endpoint` `https://mcp.canva.com/register`, `client_id_metadata_document_supported` true, PKCE `plain` and `S256`, token auth methods including `none`.
- Framer: MCP endpoint `https://mcp.framer.com/mcp`; protected-resource metadata at `https://mcp.framer.com/.well-known/oauth-protected-resource` names `https://api.framer.com/auth/oauth` and the single scope `mcp:project`; authorization-server metadata at `https://api.framer.com/.well-known/oauth-authorization-server/auth/oauth` has `registration_endpoint` `https://api.framer.com/auth/oauth/register` and PKCE `S256` only. Framer's help page (`https://www.framer.com/help/articles/getting-started-with-framer-mcp/`) describes browser OAuth sign-in and says a workspace may need admin approval for MCP connections.
- Unknown: whether either accepts an arbitrary https redirect URI on a customer's domain, and whether Framer supports the metadata-document method. Both endpoints sit on their issuer's host, so the host-binding rule of 3.3 accepts them.

### 3.10 Reference upstream with OAuth (p8)

New `scripts/upstream_mcp_oauth_reference_server.py`, a test aid like `scripts/upstream_mcp_reference_server.py` (not production): `build_app(*, base_url: str, accounts: dict[str, str]) -> Starlette`. It serves `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, `POST /register` (dynamic registration, returns a `client_id` and echoes `redirect_uris`), `GET /authorize` (checks `response_type=code`, a registered `redirect_uri`, `code_challenge_method=S256`; auto-approves and 302s to `redirect_uri` with `code`, `state` and `iss=<base_url>`), `POST /token` (`authorization_code` with PKCE check and `refresh_token` with rotation; access tokens expire in 3600 seconds, configurable by `build_app(access_token_seconds=...)`), and the MCP endpoint `/mcp` (Streamable HTTP, built like `build_app` of the existing reference server) which answers 401 with `WWW-Authenticate: Bearer resource_metadata="<base_url>/.well-known/oauth-protected-resource"` without a valid bearer. Tools: `whoami` (returns the account name the token belongs to; `accounts` maps an authorization code's chosen account, default `"alice"`) and `echo`. It never reads a claim header and fails a request that carries one.

### 3.11 Rejected alternatives (for the ADRs)

- **The SDK's `OAuthClientProvider`** (3.2).
- **A Client ID Metadata Document served by PrivacyFence** as a registration method: PrivacyFence cannot check from its side that the vendor can fetch the document, and when it cannot the person sees a vendor error page. Owner decision: left out; the cache file records the registration method so adding it needs no migration.
- **A tool list per person**: it needs a catalog and a tools-changed fan-out per principal. The shared list from the first connected person is the smallest change to ADR 0285; the cost is that a person whose account lacks a listed tool gets the vendor's own `tool_error` (Risks).
- **An operator-recorded tool snapshot**: strongest against a changing description, but toil on every setup, and it is why ADR 0300 rejected pinning schemas.
- **Sending the claim as well as the bearer**: it would leak the person's internal identity, email and audiences to a third party; refused (`identity_claim` with `auth: oauth` is an error) with a message that leaves room for an organization's own server behind its own authorization server later.
- **Encrypting tokens at rest** and **sealing them to the bearer**: ADR 0072 rejects a key stored beside the data, and refresh and scoped-token calls run with nobody present.
- **A Disconnect button**: owner decision, left out; the ADR records that a person who wants to leave removes the grant at the vendor, and that Reconnect overwrites the file.

## 4. ADRs

- **0327** An upstream can be authenticated by the person's own OAuth sign-in: the `auth` key and its refused keys, the bearer on `Authorization`, the claim never sent to such an upstream, the principal-type check in every mode (changes `identity_claim: off`), a scoped token using its creator's connection, and the OAuth declaration digest. Amends 0285, 0297, 0300.
- **0328** Discovery, client registration and storage: the metadata chain, the host-binding and address rules, declared client else dynamic registration (the metadata-document method deferred), the registration cache under the org directory, plaintext per-person tokens at 0600, the SDK helpers used and why not `OAuthClientProvider`, and no Disconnect. Relates to 0072, 0178, 0259.
- **0329** An OAuth upstream's tool list comes from the first connected person and is pinned until restart; status `waiting_for_connection`; the Admin and `privacyfence_status` fields. Amends 0285, 0319.

## 5. Manual steps

### Before implementation

None. Nothing in the phases needs a third-party account: they run against the reference server of 3.10.

### After implementation

**1. Connect a real Canva account on the QA server (about 20 minutes).** Needs a QA organization server reachable on an https address (see [The QA server](https://github.com/privacyfence/privacyfence/blob/feature/oauth-upstream-mcp/docs/connector-qa.md#the-qa-server)) running this branch, and a Canva account that holds nothing real.
1. On the QA server add to the upstream declaration (see [Upstream MCP servers](https://github.com/privacyfence/privacyfence/blob/feature/oauth-upstream-mcp/docs/upstream-mcp.md), "OAuth upstreams"): `id: canva`, `label: Canva`, `url: https://mcp.canva.com/mcp`, `auth: oauth`, and the tools you want with their gates (read tools `auto` or `review`, writes `popup`). Restart. `privacyfence_status` should show `waiting_for_connection`.
2. Open `https://<QA server>/connect`, find the **Canva** row and click **Connect**. You should land on Canva's sign-in or consent page.
3. Approve. You should return to `/connect` with `Canva connected.` and a `Connected` badge. `privacyfence_status` should now show `ready` and `connected: true`.
4. From an AI client call one read tool and one `popup` tool; the second should show an approval card.
5. Report back: pass or fail, and if step 2 fails, Canva's error text (the likely cause is that it refuses a redirect URI on this domain: then declare a `client_id` under `oauth` and put the secret in the variable named by `client_secret_env`).

**2. Connect a real Framer account on the QA server (about 20 minutes).** Same as 1 with `id: framer`, `label: Framer`, `url: https://mcp.framer.com/mcp`, `auth: oauth`; use a Framer account with a test project. If Framer asks for admin approval of MCP connections, say so in the report. Report back: pass or fail with Framer's error text.

## 6. Risks and open questions

- **Vendor redirect-URI policy is unknown** (3.9). If dynamic registration refuses our address the operator declares a `client_id`; if a vendor allows only fixed clients and no declared id exists, the feature cannot serve it. Signal: manual step 1 or 2 fails at Connect. A worker that finds the SDK helpers' signatures differ from 3.3 or 3.5 stops with `status=blocked` and quotes them.
- **The shared tool list reflects one person's account.** A person without a listed tool gets the vendor's error text (cut to 500 characters, as for any upstream). Signal: many `tool_error` rows for one OAuth upstream.
- **Principal-type enforcement for `identity_claim: off` is a behaviour change.** Only operators who set `allowed_principal_types` to one type and use `off` are affected, and the docs already promise enforcement. If p5 finds another test pinning the old behaviour, update it to the new rule; never loosen the rule.
- **A scoped token acts as its creator at the vendor** (owner's decision); ADR 0300's tick still gates writes. The token form says so (3.8).
- **Refresh-token rotation across processes**: the per-path lock is in-process; the org server is one process. If it ever runs more than one, add a file lock (the `upstream_claim_keys.py` precedent).
- **The web loop**: `on_connected` sets an `asyncio.Event` from a route handler and the lifespan waits on it; both run in the web server's event loop. If p5 finds the lifespan runs in another loop, it stops with `status=blocked`.
- **A reverse proxy that logs the callback query** keeps the code (PKCE bounds the damage); the docs say so.
- **A declared client secret in an environment variable** is visible to anything that can read the daemon's environment; the docs say to use the orchestrator's secret store.
- **Protected paths**: no phase touches `.tld/toolkit.yaml` or `.github/workflows/**`.

## Implementation manifest

```yaml
plan_slug: oauth-upstream-mcp
feature_branch: feature/oauth-upstream-mcp
tracking_issue: 1155
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-connect-canva
    title: Connect a real Canva account on the QA server and call a read and a gated tool
    why: CI runs only against the reference OAuth server; only a real Canva shows whether its registration accepts this server's redirect URI and what its tools answer.
  - id: ma2-connect-framer
    title: Connect a real Framer account on the QA server and call a tool
    why: Framer's registration policy and its admin approval of MCP connections cannot be checked from CI.
verify_after_merge:
  - python3 -m pytest tests/unit tests/integration -q -k upstream
final_checks:
  - "`tld plan deviations check --file docs/1155/deviations.md --plan docs/1155/plan.md --all` exits 0"
  - ADRs 0327, 0328 and 0329 exist, are Accepted, and are in docs/adr/README.md
  - CHANGELOG.md has an entry under `## [Unreleased]` and no new version heading
phases:
  - id: p1-config-keys
    title: The auth and oauth operator-file keys, the sends_claim predicate and the OAuth digest
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_config.py
      - tests/unit/test_upstream_mcp_config.py
    brief: |
      Implement design section 3.1 of docs/1155/plan.md exactly (key names, limits and problem texts are fixed there). The pattern for a new key and its tests is commit 0f4910958 (audiences_tool) and the email_for_visibility tests.
      1. BEFORE editing, run python3 -c with the current code to compute declaration_digest for the sample upstream the existing digest tests use (copy its construction from tests/unit/test_upstream_mcp_config.py::TestDeclarationDigest) and note the 32-character result; it becomes the literal in test_a_claim_upstream_digest_is_unchanged.
      2. src/privacyfence/upstream_mcp_config.py: add "auth" and "oauth" to _UPSTREAM_KEYS; add the frozen dataclass UpstreamOAuthSettings and the fields UpstreamServer.auth ("claim") and UpstreamServer.oauth (None) after audiences_refresh_seconds, plus the property sends_claim. In _parse_one: read auth first; when "oauth", refuse each of audience, identity_claim, claim_lifetime_seconds, email_for_visibility, audiences_tool, audiences_refresh_seconds if present with "is not used with auth oauth", skip the audience requirement, apply the oauth address rule instead of _url_ok (https, or http for localhost or a loopback IP only), parse the oauth object with the rules and texts of the 3.1 table (an absent oauth gives UpstreamOAuthSettings()), and build the UpstreamServer with audience "", identity_claim "off", claim_lifetime_seconds 60, email_for_visibility False, audiences_tool None. When "claim", an "oauth" key is refused with "needs auth oauth". The oauth.issuer rule uses urlsplit: scheme https (or http with a loopback host), no username, password, query or fragment.
      3. declaration_digest: for auth "claim" keep the pinned dict untouched; for auth "oauth" use the dict of 3.1 (sorted scopes list).
      4. Update the module docstring (one sentence naming auth oauth). The docs are the retirement phase's.
      5. Tests in tests/unit/test_upstream_mcp_config.py: test_auth_defaults_to_claim, test_auth_oauth_needs_no_audience, test_auth_oauth_refuses_each_claim_key (parametrized over the six keys), test_auth_rejects_an_unknown_value, test_oauth_block_needs_auth_oauth, test_oauth_unknown_key_is_refused, test_oauth_scopes_rules (0, 21, a whitespace scope, 101 characters refused; one scope read), test_oauth_client_id_rules, test_oauth_client_secret_env_rules (lowercase refused, needs client_id), test_oauth_issuer_rules (query, fragment, userinfo, plain http to a non-loopback host refused; a path accepted), test_oauth_url_must_be_https_or_loopback (single-word host refused), test_oauth_upstream_has_sends_claim_false, test_claim_upstream_sends_claim_only_when_required, test_a_claim_upstream_digest_is_unchanged (the literal from step 1), test_an_oauth_digest_changes_with_scopes_client_id_and_issuer, test_an_oauth_digest_ignores_the_order_of_scopes.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_config.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_config.py -q --cov=privacyfence.upstream_mcp_config --cov-branch --cov-report=term-missing reports no missing line in the new code
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "is not used with auth oauth" src/privacyfence/upstream_mcp_config.py matches
  - id: p2-oauth-tokens
    title: The token module - errors, token files, authorize URL, code exchange, pending store, token provider and bearer auth
    depends_on: [p1-config-keys]
    complexity: M
    touches:
      - src/privacyfence/upstream_oauth_tokens.py
      - tests/unit/test_upstream_oauth_tokens.py
      - tests/unit/test_systemic_gate_invariants.py
    brief: |
      Implement design sections 3.2 and 3.5 of docs/1155/plan.md exactly. Read src/privacyfence/miro_auth.py (save_token_file, MiroTokenProvider, _token_request) and src/privacyfence/identity_claim.py (IdentityClaimAuth) as patterns; read mcp/shared/auth.py (OAuthToken) and mcp/shared/auth_utils.py (calculate_token_expiry) in the installed SDK first.
      1. Create src/privacyfence/upstream_oauth_tokens.py with a module docstring (what it is; ADR 0327, 0328) and: UpstreamOAuthError(kind: str) with .kind and the kinds not_connected, auth_expired, unreachable, invalid_metadata, not_configured, registration_failed, insufficient_scope (str(exc) is the kind only); frozen dataclasses OAuthClient(client_id, client_secret, auth_method) and TokenRecord and PendingUpstreamAuth; origin_of(url); token_path, token_paths, load_token, save_token, delete_token; build_authorize_url; async exchange_code; PendingUpstreamAuthStore; UpstreamTokenProvider; BearerAuth. Signatures, fields and behaviours are those of 3.5. No module-level mutable state.
      2. All HTTP uses httpx2.AsyncClient(transport=transport, timeout=httpx2.Timeout(10.0)) via a private helper that streams the body and stops at 64 KiB (more raises UpstreamOAuthError("unreachable")). No exception, log line or message contains a response body, a token, a code or a secret; log lines name the upstream id and the kind only.
      3. Add ("upstream_oauth_tokens", None, "save_token") to TOKEN_WRITE_SITES in tests/unit/test_systemic_gate_invariants.py and make save_token write through secure_files.atomic_write_json so that invariant passes; read the test first and follow what it checks for the other entries.
      4. Tests in tests/unit/test_upstream_oauth_tokens.py with httpx2.MockTransport: token file round trip and mode 0600 and 0700 directory; load_token on an absent, malformed and wrong-version file returns None; token_paths orders newest first and ignores other upstream ids; build_authorize_url contains each parameter and omits scope and resource when None; exchange_code success for each of the three auth methods (assert the Authorization or form fields), expires_in absent gives expires_at None, a non-bearer token_type refused, 400 gives auth_expired, 500 gives unreachable, an oversized body gives unreachable, and no error text contains the code, verifier or secret (sentinel strings); PendingUpstreamAuthStore single use, TTL with a fake clock, max_entries drop of the oldest; provider: a fresh token is returned without a request, a token within 60 seconds is refreshed and saved, a rotated refresh token replaces the old and an absent one keeps it, two concurrent bearer calls refresh once, force_refresh refreshes, a 400 deletes the file and raises auth_expired, a 500 keeps the file and raises unreachable, a returned different client_id raises auth_expired and deletes, a stale token without refresh token raises auth_expired, an absent file raises not_connected, expires_at None never refreshes; BearerAuth adds the header only on the matching origin, records status, and sets insufficient_scope for both header spellings; test_the_sdk_names_this_module_uses_exist imports OAuthToken from mcp.shared.auth and calculate_token_expiry from mcp.shared.auth_utils.
      5. Stop condition: if calculate_token_expiry or OAuthToken differ from what 3.5 assumes (read them), stop with status=blocked and quote the signatures.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_oauth_tokens.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit/test_upstream_oauth_tokens.py -q --cov=privacyfence.upstream_oauth_tokens --cov-branch --cov-report=term-missing reports 100%
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "class UpstreamTokenProvider" src/privacyfence/upstream_oauth_tokens.py matches
  - id: p3-oauth-discovery
    title: The discovery and client-registration module
    depends_on: [p2-oauth-tokens]
    complexity: M
    touches:
      - src/privacyfence/upstream_oauth_discovery.py
      - tests/unit/test_upstream_oauth_discovery.py
    brief: |
      Implement design sections 3.3 and 3.4 of docs/1155/plan.md exactly (the order of steps, the checks and the refusal kinds are fixed there). Read src/privacyfence/grist_auth.py (discover, _endpoint, _discovery_document) as a pattern, and read these installed-SDK helpers before using them: mcp.client.auth.utils build_protected_resource_metadata_discovery_urls, build_oauth_authorization_server_metadata_discovery_urls, extract_resource_metadata_from_www_auth, validate_metadata_issuer; mcp.shared.auth ProtectedResourceMetadata, OAuthMetadata, OAuthClientInformationFull; mcp.shared.auth_utils check_resource_allowed, resource_url_from_server_url.
      1. Create src/privacyfence/upstream_oauth_discovery.py with a module docstring (ADR 0328), the frozen dataclass OAuthEndpoints, the private _checked_url(url, issuer_host, loopback) of 3.3 step 1, async discover(upstream, *, transport=None), and class RegistrationStore with ensure_client, client_for and forget (3.4). Import UpstreamOAuthError, OAuthClient and the HTTP helper from upstream_oauth_tokens. No module-level mutable state; the per-upstream asyncio.Lock dict is an instance attribute. Never use the SDK fallbacks named in 3.3 step 10; never use the helper create_client_registration_request (build the registration request here, 3.4).
      2. The registration cache directory is the constructor argument; files are written by a method named _write with secure_files.atomic_write_json after secure_files.secure_mkdir, mode 0600. This is a client registration, not a user token, so it is not added to TOKEN_WRITE_SITES.
      3. Tests in tests/unit/test_upstream_oauth_discovery.py with httpx2.MockTransport servers: the happy path for a Canva-shaped server (issuer = resource origin) and a Framer-shaped one (issuer with a path on another host than the MCP URL, metadata at the path-inserted well-known URL); a 401 hint is followed; no hint falls back to the well-known URLs; step 4 refusals (mismatching resource, missing resource); pinned issuer not listed refused, pinned issuer listed used; authorization_servers empty refused; issuer mismatch in the metadata refused; each endpoint on another host refused, on another port refused, http refused unless the upstream URL is loopback http, an IP literal in each private range (10.0.0.1, 127.0.0.1, 169.254.169.254, 192.168.1.1, ::1, fc00::1) refused unless loopback, userinfo and fragment refused, a single-label host and a .internal host refused; S256 missing or the list absent refused; scope selection (declared, scopes_supported, none); token_auth_methods default; redirects are not followed (a 302 answer is a refusal); a body over 64 KiB refused; a transport error is unreachable; the SDK fallbacks are never requested (assert the recorded request URLs). RegistrationStore: declared client_id with and without a secret (environment variable via monkeypatch), declared name with an empty variable not_configured, a first registration writes the file with mode 0600 and sends the exact JSON body of 3.4 for each of the three method choices, a second call with the same issuer and redirect URI sends no request, a changed redirect URI or issuer registers again, an expiring secret registers again, concurrent ensure_client calls register once, a registration answer without client_id or with other redirect_uris is registration_failed, no registration_endpoint without a declared id is registration_failed, client_for returns declared, cached, and raises not_configured otherwise, forget removes the file. test_the_sdk_names_this_module_uses_exist imports every SDK name listed above.
      4. Stop condition: if a helper's signature differs from `build_protected_resource_metadata_discovery_urls(www_auth_url, server_url)`, `build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, server_url)`, `extract_resource_metadata_from_www_auth(response)`, `validate_metadata_issuer(oauth_metadata, expected_issuer)`, stop with status=blocked and quote the signature.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_oauth_discovery.py -q passes
      - python3 -m pytest tests/unit/test_upstream_oauth_discovery.py -q --cov=privacyfence.upstream_oauth_discovery --cov-branch --cov-report=term-missing reports 100%
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "def _checked_url" src/privacyfence/upstream_oauth_discovery.py matches
  - id: p4-client-connector
    title: The bearer path in the upstream client and connector, and the claim predicate
    depends_on: [p2-oauth-tokens]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_client.py
      - src/privacyfence/connectors/upstream_mcp.py
      - tests/unit/test_upstream_mcp_client.py
      - tests/unit/connectors/test_upstream_mcp_connector.py
    brief: |
      Implement the first two bullets of design section 3.6 of docs/1155/plan.md exactly (the third bullet, the runtime's admit_caller, is p5; here the connector only calls the admit callable it is given).
      1. src/privacyfence/upstream_mcp_client.py: define BearerSource = Callable[[bool], Awaitable[str]]; add the keyword-only bearer parameter to list_tools and call_tool; in _run build the claim auth only when upstream.sends_claim (replace the `identity_claim == "required"` test), and for upstream.auth == "oauth" get the token with await bearer(False) (None raises UpstreamMcpClientError("not_connected"); an UpstreamOAuthError from the source raises UpstreamMcpClientError(exc.kind) for the kinds not_connected, auth_expired, insufficient_scope and "unreachable" for every other kind), send it with BearerAuth(token, origin_of(upstream.url)) and apply the phase rule of 3.6 (one retry with bearer(True) on a fresh session for a 401 before initialize finished; a 401 during the call raises auth_expired and the call is not sent again; a 403 with insufficient_scope raises insufficient_scope). Update the UpstreamMcpClientError docstring with the new kinds.
      2. src/privacyfence/connectors/upstream_mcp.py: add the optional admit and bearer constructor arguments, store them, call admit() at the top of _forward when given (CallerNotAllowedError keeps its present handling and status "refused"), pass bearer() to the client for an OAuth upstream, run the caller callable only when upstream.sends_claim, map the three new kinds to UpstreamUnavailableError with the exact messages of 3.6, add "auth": upstream.auth to the _forward_audit metadata. Update the module docstring (one paragraph naming the OAuth path).
      3. Tests. Client (tests/unit/test_upstream_mcp_client.py, using the transport pattern of the existing tests): an OAuth upstream sends Authorization: Bearer on initialize, the call and the close, and never an X-PrivacyFence-Claim header (assert on every recorded request); a bearer of None raises not_connected; each mapped UpstreamOAuthError kind; a 401 on initialize retries once with force_refresh True and then succeeds; a second 401 raises auth_expired; a 401 on tools/call raises auth_expired and the call is recorded exactly once; a 403 insufficient_scope raises insufficient_scope; the bearer is not sent to a request whose origin differs (a redirect target is not followed). Claim upstreams behave exactly as before (the existing tests pass unchanged). Connector (tests/unit/connectors/test_upstream_mcp_connector.py): the three messages verbatim; admit raising CallerNotAllowedError gives UpstreamCallRefusedError for a claim upstream with identity_claim off AND for an OAuth upstream, and the client is never touched; the caller callable is not called for an OAuth upstream; the audit row has metadata auth; update the test at line 303 so that, with an admit callable that raises, an identity_claim off upstream is refused (state in its docstring that this is the ADR 0327 behaviour).
      4. Stop condition: if the SDK's streamable_http_client does not pass the httpx2 client's auth to every request (initialize, POST, DELETE), stop with status=blocked and quote what you saw.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_client.py tests/unit/connectors/test_upstream_mcp_connector.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_client.py tests/unit/connectors/test_upstream_mcp_connector.py -q --cov=privacyfence.upstream_mcp_client --cov=privacyfence.connectors.upstream_mcp --cov-branch --cov-report=term-missing reports no missing line in the new code
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "sends_claim" src/privacyfence/upstream_mcp_client.py src/privacyfence/connectors/upstream_mcp.py matches in both files
  - id: p5-runtime
    title: The runtime for OAuth upstreams - admit_caller, who sees the tools, the tool list from a connection, status and Admin rows
    depends_on: [p3-oauth-discovery, p4-client-connector]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_runtime.py
      - tests/unit/test_upstream_mcp_runtime.py
    brief: |
      Implement design section 3.7 and the third bullet of 3.6 of docs/1155/plan.md exactly. Read the existing tests/unit/test_upstream_mcp_runtime.py (the configured fixture, _FlakyClient, TestLifespan with the patched _sleep) before editing.
      1. src/privacyfence/upstream_mcp_runtime.py: add the _Entry fields and build them in configure for OAuth upstreams (including the ConfigurationError for an empty client_secret_env variable and the groups-ignored warning of 3.7; configure keeps its present signature and the transport argument is passed to RegistrationStore and UpstreamTokenProvider); add admit_caller and make caller_for call it; pass admit=functools.partial(admit_caller, upstream) for every upstream and bearer=functools.partial(bearer_for_current, entry) for OAuth ones in connectors_for_principal, and return an OAuth upstream's connector only when the current principal has a token file; add on_connected, has_connection, oauth_upstream, registrations_for(upstream_id) (returns the RegistrationStore, used by the routes) and tokens/ pending accessors only if the routes need them (they do not: the routes build their own PendingUpstreamAuthStore); add _list_with_a_connection and make lifespan start it for OAuth upstreams instead of _fetch_tool_list; extend status() and admin_rows() as 3.7 says; make held_audiences, all_group_audiences and audiences_for_email skip OAuth upstreams.
      2. The event: connected_event is an asyncio.Event created in configure. Stop condition: lifespan and the route handlers must run in the same event loop; if you find they do not (read web/server.py and the lifespan wiring), stop with status=blocked.
      3. The audit entry of the listing uses get_audit_logger().record(AuditEntry(...)) as _audit_audiences does, with decision "upstream_tools_listed" and an empty request id; a failing write changes nothing.
      4. Tests in tests/unit/test_upstream_mcp_runtime.py (fake token files written with upstream_oauth_tokens.save_token under tmp_path with paths.data_dir patched like the other tests do): admit_caller refuses n8n_workflow when only claude_client is allowed and the reverse, for claim and OAuth upstreams, and for identity_claim off; connectors_for_principal omits an OAuth connector for a person with no token file and includes it with one; the lifespan lists with the newest candidate, skips a failing candidate and uses the next, waits when there is none (patched wait) and lists after on_connected; the pinned list is not fetched again; undeclared tool names are logged cleaned and cut; the audit entry is written and holds no person; status rows for the three OAuth states (waiting_for_connection, waiting_for_tool_list, ready), connected true and false with the hint text verbatim, claim upstreams unchanged (no connected key); admin_rows carry auth and the oauth block and never a client id or token; held_audiences and all_group_audiences skip OAuth; configure raises ConfigurationError with the exact text for an empty secret variable.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_runtime.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_runtime.py -q --cov=privacyfence.upstream_mcp_runtime --cov-branch --cov-report=term-missing reports no missing line in the new code
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "waiting_for_connection" src/privacyfence/upstream_mcp_runtime.py matches
  - id: p6-web-routes
    title: The connect and callback routes, the Connect-page rows and the server wiring
    depends_on: [p5-runtime]
    complexity: M
    touches:
      - src/privacyfence/web/routes_upstream_oauth.py
      - src/privacyfence/web/routes_connect.py
      - src/privacyfence/web/server.py
      - tests/unit/web/test_routes_upstream_oauth.py
      - tests/unit/web/test_routes_connect.py
    brief: |
      Implement design section 3.8 (routes, Connect page, token-page field) of docs/1155/plan.md exactly. Read src/privacyfence/web/routes_connect.py (start, callback, _PendingAuthStore, _service_row_html, _flash_html, _render_connect_page, connect_page) and tests/unit/web/test_routes_connect.py (TestMiroOAuth, TestRelay's app helpers) first; the pattern for the callback checks is the generic callback (ADR 0178).
      1. Create src/privacyfence/web/routes_upstream_oauth.py with build_routes(*, sessions, connector_registry, issuer_url, transport=None) and connect_section_html(principal, flash_connected_id, flash_error_id). Use upstream_mcp_runtime.oauth_upstream(id), registrations_for(id), on_connected(id), has_connection(id) (has_connection must be evaluated for the given principal: use upstream_oauth_tokens.load_token(token_path(principal, id)) in connect_section_html instead, since that function is called with an explicit principal) and a PendingUpstreamAuthStore created inside build_routes. The redirect URI is issuer_url.rstrip("/") + "/upstream-mcp/<id>/callback". Texts, redirects, no-store header and logging rules are those of 3.8; HTML-escape every label.
      2. routes_connect.py: read upstream_connected and upstream_error in connect_page, pass them to _render_connect_page (new keyword arguments with default ""), and place the flash and the rows as 3.8 says; the function-local import of routes_upstream_oauth avoids a cycle. No other behaviour of routes_connect changes.
      3. web/server.py: mount routes_upstream_oauth.build_routes(...) right after routes_connect.build_routes inside the `if org.connector_registry is not None:` block, only when upstream_mcp_runtime.configured(); add "auth": row["auth"] to each entry of page["upstreams"] (around line 335).
      4. Tests in tests/unit/web/test_routes_upstream_oauth.py (helpers copied in style from test_routes_connect.py; the upstream is served by an httpx2.MockTransport passed as transport, the runtime configured as the runtime tests do): connect without a session redirects to /login; an unknown id and a claim upstream give 404; connect redirects to the authorization endpoint with state, S256 challenge, redirect_uri, client_id, scope and resource and Cache-Control no-store; a discovery failure redirects to /connect?upstream_error=<id>; callback with the right state saves the token file (mode 0600, fields of 3.5), evicts the registry entry, sets the runtime event, and redirects to ?upstream_connected=<id>; callback with another principal's session, an expired state, a reused state, a state of another upstream, a provider error, a missing code, a mismatching iss, and a failing exchange all save nothing and redirect or answer as 3.8 says; the logs of these cases (caplog) contain neither the state, the code, nor a token nor secret sentinel; connect_section_html shows Connect for a person with no token and Reconnect with the Connected badge for one with a token, escapes a label such as <b>x</b>, and the flash texts verbatim. In tests/unit/web/test_routes_connect.py add test_the_connect_page_lists_an_oauth_upstream and test_an_unknown_upstream_flash_value_is_ignored. Add one test in the new file that the token page entry has "auth" (call the server helper that builds it, found by grep of "gated_tools" in web/server.py).
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_upstream_oauth.py tests/unit/web/test_routes_connect.py -q passes
      - python3 -m pytest tests/unit/web/test_routes_upstream_oauth.py -q --cov=privacyfence.web.routes_upstream_oauth --cov-branch --cov-report=term-missing reports 100%
      - ruff check . and python3 -m mypy src/privacyfence pass
      - python3 -m pytest tests/unit -q -k "server_org_mode or routes_connect" passes
  - id: p7-settings-ui
    title: The scoped-token form and Admin card for OAuth upstreams
    depends_on: [p6-web-routes]
    complexity: S
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Implement the last bullet of design section 3.8 of docs/1155/plan.md. Read scopedAudiencesHtml (about line 1296) and upstreamCardHtml (about line 2570) in src/privacyfence/settings_window_html.py, and TestOrgAdminSections (test_upstreams_ready_lists_tools_and_groups, _render_org, _org_state) in tests/unit/test_settings_window_html.py first.
      1. scopedAudiencesHtml: for an upstream whose `auth` is the string 'oauth' emit `<fieldset class="pf-st-audiences" data-st-audiences-for="<id>"><legend class="pf-row-title">Connection for <label></legend><div class="pf-hint">This token acts through your own <label> connection: it can do what your account there can do.</div></fieldset>` (labels escaped with esc) and no chips; other upstreams unchanged. scopedChosenAudiences needs no change (an OAuth upstream has no audiences).
      2. upstreamCardHtml: for a row with `auth === 'oauth'` the summary card replaces the 'Claim audience' and 'Identity claim' rows with one row 'Sign-in' whose text is 'OAuth' + (u.oauth.issuer_host ? ' at ' + u.oauth.issuer_host : '') + ', ' + u.oauth.connected_people + ' person(s) connected, client ' + u.oauth.registration + '.' and keeps the 'Who may call it' row; the state text for u.state === 'waiting_for_connection' is 'Waiting for a person to connect. AI clients see none of its tools until someone does.'; and the function returns the heading, summary and tool card only (no group card and no audiences card) when u.audiences_source === 'none'.
      3. Tests: a scoped-form test that an OAuth upstream renders the 'Connection for' legend and hint and no data-st-audience chip, and that a claim upstream still renders 'Audiences for'; Admin tests for the Sign-in row, the waiting_for_connection text, and the absence of 'groups and audiences' for an OAuth row; follow the existing test helpers.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_window_html.py -q passes
      - ruff check . passes
      - grep -n "Connection for" src/privacyfence/settings_window_html.py matches
  - id: p8-oauth-reference-server
    title: A reference upstream with OAuth, for tests and a development setup
    depends_on: []
    complexity: M
    touches:
      - scripts/upstream_mcp_oauth_reference_server.py
      - tests/unit/test_upstream_mcp_oauth_reference_server.py
    brief: |
      Implement design section 3.10 of docs/1155/plan.md exactly. Read scripts/upstream_mcp_reference_server.py (build_app, _build_server, the session manager and lifespan wiring) and copy its way of mounting the MCP endpoint; do not edit that file. Use only the standard library, starlette and mcp already installed; the module is a script like the existing one (module docstring stating it is a test aid, not production, and how to run it with python3 scripts/upstream_mcp_oauth_reference_server.py --base-url http://127.0.0.1:8098 --port 8098).
      1. build_app(*, base_url, accounts=None, access_token_seconds=3600) -> Starlette with the routes of 3.10: both well-known documents (protected resource: resource "<base_url>/mcp", authorization_servers ["<base_url>"], scopes_supported ["mcp:read"]; authorization server: issuer base_url, authorization_endpoint, token_endpoint, registration_endpoint, code_challenge_methods_supported ["S256"], token_endpoint_auth_methods_supported ["none"], authorization_response_iss_parameter_supported true), POST /register, GET /authorize, POST /token, and /mcp. State (registered clients, codes, tokens) lives in the app object, never at module level. A code is single-use and bound to client_id, redirect_uri and the PKCE challenge; refresh rotates; an expired or unknown access token on /mcp gives 401 with the WWW-Authenticate header of 3.10. Tools: whoami (text = the account name of the token) and echo (text = its `text` argument). A request with an X-PrivacyFence-Claim header gets a 400 so a test notices a leaked claim.
      2. main() with argparse (--base-url, --host default 127.0.0.1, --port default 8099) running uvicorn like the existing script does.
      3. Tests in tests/unit/test_upstream_mcp_oauth_reference_server.py using httpx2 against the ASGI app (the pattern of tests/unit/test_upstream_mcp_reference_server.py): both documents are served; a full dynamic registration, authorize (assert the redirect carries code, state and iss), token exchange with a correct and a wrong verifier, refresh rotation (the old refresh token no longer works), an expired token gets 401 with the header, a valid token initializes and lists the two tools through the mcp client, whoami returns the account, a claim header gets 400, a reused code is refused.
      4. Stop condition: if the installed mcp lowlevel server cannot be mounted behind a bearer check the way the existing script mounts it, stop with status=blocked and quote the error.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_oauth_reference_server.py -q passes
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "authorization_response_iss_parameter_supported" scripts/upstream_mcp_oauth_reference_server.py matches
  - id: p9-integration
    title: An organization server with an OAuth upstream, end to end
    depends_on: [p6-web-routes, p8-oauth-reference-server]
    complexity: M
    touches:
      - tests/integration/test_upstream_oauth_org.py
    brief: |
      Read tests/integration/test_upstream_mcp_org.py (the stack fixture at line 91 and the helper functions at lines 53-90, the token_stack fixture at 290) and docs/testing-policy.md (which tier this is) first; copy the stack fixture's way of starting a real organization server on a free port, here with the OAuth reference server of scripts/upstream_mcp_oauth_reference_server.py started on its own free port (import it by path the way the existing integration test imports scripts/upstream_mcp_reference_server.py) and an operator file declaring it with auth oauth (id "oauthref", read tools with gate auto, one write tool gate popup if the reference server has one, otherwise only whoami and echo).
      Tests (a person is simulated with httpx2: sign in through the org server's test login the way the stack fixture does, call /upstream-mcp/oauthref/connect without following redirects, follow the authorize redirect on the reference server, then GET the callback on the org server with the returned query):
      1. test_before_anyone_connects_the_status_says_waiting_for_connection_and_no_tool_is_listed.
      2. test_a_person_connects_and_calls_a_tool_as_themselves: after the callback privacyfence_status shows ready and connected true for that person, tools/list through /mcp contains oauthref_whoami, and calling it returns that person's account.
      3. test_a_second_person_has_no_tool_until_they_connect: another signed-in person's tools/list has no oauthref tool and, after connecting, has them.
      4. test_an_expired_access_token_is_refreshed (build_app(access_token_seconds=1) and a short sleep, then a call succeeds, and the token file holds a new access token).
      5. test_no_claim_header_reaches_the_oauth_upstream (the reference server rejects a claim header with 400, so every call above already proves it; this test additionally asserts the audit rows have metadata auth oauth and an empty claim_jti).
      6. test_a_revoked_connection_gives_the_expired_message: make the reference server forget its tokens, call a tool, expect the message "The oauthref sign-in has expired" prefix with label as declared, and the token file removed.
      Keep it under 60 seconds in total. Do not touch other files.
    acceptance:
      - python3 -m pytest tests/integration/test_upstream_oauth_org.py -q passes
      - python3 -m pytest tests/integration/test_upstream_mcp_org.py -q passes
      - ruff check . passes
  - id: p10-retire
    title: ADRs 0327-0329, reference docs and the changelog
    depends_on: [p7-settings-ui, p9-integration]
    complexity: M
    touches:
      - docs/adr/0327-an-upstream-can-be-authenticated-by-the-persons-own-oauth-sign-in.md
      - docs/adr/0328-oauth-upstreams-discover-their-server-register-a-client-and-keep-tokens-per-person.md
      - docs/adr/0329-an-oauth-upstreams-tool-list-comes-from-the-first-connected-person.md
      - docs/adr/README.md
      - docs/adr/0285-upstream-mcp-servers-are-declared-connectors.md
      - docs/adr/0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md
      - docs/adr/0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md
      - docs/adr/0319-the-admin-page-shows-upstream-mcp-servers-read-only.md
      - docs/upstream-mcp.md
      - docs/identity-claim.md
      - docs/configuration-reference.md
      - docs/connecting-a-service.md
      - docs/org-mode-docker.md
      - docs/connector-qa.md
      - CHANGELOG.md
    brief: |
      Write the ADRs and update the docs from design sections 3.1-3.11 and 4 of docs/1155/plan.md, using what the code now does (read src/privacyfence/upstream_mcp_config.py, upstream_oauth_discovery.py, upstream_oauth_tokens.py, upstream_mcp_runtime.py and web/routes_upstream_oauth.py to quote real names). Pattern commit for ADRs and docs: 0f4910958. docs/adr/README.md explains the ADR template and status wording: read its template section first.
      1. Three ADRs with exactly the files and decisions of section 4 (the file names above are fixed; the titles in the README rows are the decisions' one-line statements): each has Status (Accepted — 2026-11-18, Implemented, and the Amends/Relates lines of section 4), Context (issue #1155 link), Decision, Alternatives considered (from 3.11), Consequences, Verification (the test files of p1-p9), Related. ADR 0327 records the change to `identity_claim: off` and the owner decisions on scoped tokens and on no Disconnect; ADR 0328 records that the metadata-document method was left out by the owner's decision and that the registration file records the method; ADR 0329 records the Admin and status fields.
      2. docs/adr/README.md: three index rows in the existing format, and the "amended by" text on the rows of 0285 (0327, 0329), 0297 (0327), 0300 (0327), 0319 (0329). Add one "Amended by ADR 0327/0329" line each to the Status block of the four older ADR files, and change no other word of an accepted ADR.
      3. docs/upstream-mcp.md: a new "### OAuth upstreams" section after "The operator file" (the keys of 3.1 with a YAML example for Canva and one for Framer, the Connect step for a person, the states `waiting_for_connection`, the message texts of 3.6, the scoped-token rule, the shared tool list, that groups and audiences do not apply, the reverse-proxy and secret-variable cautions of section 6); fix line 83's "who may call it" statement and the "It never sets `Authorization`" sentence (line 309) to say it is so for claim upstreams; add the new keys to the "Operator keys" table and the new status and Admin fields to their sections.
      4. docs/identity-claim.md line 21: Authorization is never set for an upstream that uses the claim; an OAuth upstream gets no claim. docs/configuration-reference.md: the `auth` and `oauth` keys in the PRIVACYFENCE_UPSTREAM_MCP_FILE row. docs/connecting-a-service.md: one paragraph that an OAuth upstream appears on the Connect page. docs/org-mode-docker.md: one example in "Upstream MCP file" and the secret variable note. docs/connector-qa.md: the two manual QA checks of section 5 (Canva, Framer) as a short subsection.
      5. CHANGELOG.md under `## [Unreleased]` (never a version heading): "- **Hosted MCP servers with their own sign-in.** An operator can declare an upstream with `auth: oauth` (for example Canva or Framer); each person connects their own account on the Connect page, PrivacyFence sends their token and never the identity claim, and the tools keep their gates, cards, scoped-token rules and audit entries. The allowed-callers setting of an upstream is now enforced for every upstream, including one with `identity_claim: off` ([ADR 0327](docs/adr/0327-an-upstream-can-be-authenticated-by-the-persons-own-oauth-sign-in.md), [#1155](https://github.com/privacyfence/privacyfence/issues/1155))."
      6. Without docs.index in the profile, no docs-index step applies; run tld ops docs-index check only if docs/README.md lists docs this phase adds (it adds none).
    acceptance:
      - ls docs/adr/0327-*.md docs/adr/0328-*.md docs/adr/0329-*.md lists three files
      - grep -c "0327\|0328\|0329" docs/adr/README.md reports at least 3
      - grep -n "auth: oauth" docs/upstream-mcp.md matches
      - grep -n "Hosted MCP servers with their own sign-in" CHANGELOG.md matches under the Unreleased heading
      - ruff check . passes
```
