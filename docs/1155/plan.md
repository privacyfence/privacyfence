# Plan: OAuth-authenticated hosted upstream MCP servers, replacing the claim-based upstreams (issue 1155)

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator declares a hosted MCP server (`id`, `label`, `url`, optional `oauth` settings, for example Canva or Framer) in the upstream file and restarts. Each person clicks **Connect** for it on `/connect` and signs in at the vendor. An administrator who has connected opens **Admin → Upstream MCP servers**, presses **Refresh tools**, reviews the list (a tool that says it is read-only and not destructive defaults to `review`, every other tool to `popup`), may set any tool to `auto`, and saves. The tools then reach connected people as `<id>_<tool>`. `auto` is only a ceiling: the tool keeps asking until a person makes an Always allow rule for that one tool. A scoped-token form note names the connection a token uses.

**Scope:** The claim-based upstream feature is removed completely (identity claim, its keys and JWKS, audiences, group maps and approvals, the group-membership tool, the reference server, the verifier, the docs) and replaced by this OAuth-only one; whatever used it, the `kh_*` tools included, stops working after the upgrade, and existing scoped-token approvals of upstream tools become void. The operator file no longer declares tools. Old ADRs are marked superseded, not deleted. Not built: a Disconnect button, the Client ID Metadata Document method.

**Security:** An upstream never receives a PrivacyFence claim, and the bearer goes only to the declared server's origin. Metadata endpoints must be https, on the issuer's host, and the issuer must resolve to public addresses (3.3). Tokens are plaintext 0600 JSON per person. Tool annotations are untrusted, so only `readOnlyHint=true` earns `review`. A new tool stays hidden, and a changed tool keeps its saved version, until an administrator saves (3.6). A scoped token acts through its creator's connection. Policy tables are rewritten live under a lock (3.7).

**Review focus:** the breaking removal and what it leaves on disk (3.1, 5); the default-gate rule from untrusted annotations with the hold-until-save pin (3.6); the per-tool `auto` ceiling, its stored rule and live re-registration of policy tables (3.7); scoped tokens acting through their creator's connection (3.7, 3.8).

## 1. Goal

People should be able to use a hosted third-party MCP server that signs each person in with its own OAuth (the MCP authorization spec), such as Canva or Framer, through PrivacyFence, with cards, scoped tokens, auto-accept rules and the audit log applying to its tools. The claim-based upstream feature (ADR 0285 and its amendments), which only suits a server that verifies PrivacyFence's signed claim, is dropped. Issue: https://github.com/privacyfence/privacyfence/issues/1155.

Owner decisions (board asks `a-7dfd02cfe96489be` and `a-2cc85f26a2a8f635`, 2026-11-18 and 2026-11-19): client registration is a declared `client_id`, else RFC 7591 dynamic registration; a scoped token may call through its creator's connection; no Disconnect button; read-only tools default to `review` and write or destructive tools to `popup`, classified only by the server's `readOnlyHint=true`; tools new or changed after Refresh stay hidden until the administrator saves; the Always allow button creates one rule per tool; the removal of the claim feature is confirmed, including its breaking effect. A research report (`r-20261118-1`, 2026-11-18) found both vendors publish RFC 9728 metadata and a registration endpoint (3.9).

## 2. Current state

Measured by two read-only sweeps of `main` at `f6c95e6b0` (that commit is the reference for any file a brief says to copy from: `git show f6c95e6b0:<path>`).

- **The feature to remove** is about 3,900 lines in 15 source files plus `contrib/verifier/`, `contrib/examples/knowledge-hub/`, `scripts/upstream_mcp_reference_server.py`, 17 claim-only test files (about 4,850 lines), 11 ADRs and two docs (`docs/identity-claim.md`, `docs/upstream-mcp.md`). Its source: `identity_claim.py`, `upstream_claim_keys.py`, `upstream_audiences.py`, `group_membership.py`, `connectors/group_membership.py`, `web/routes_upstream_mcp.py` and the core `upstream_mcp_config.py`, `upstream_mcp_registry.py`, `upstream_mcp_runtime.py`, `upstream_mcp_client.py`, `connectors/upstream_mcp.py` (all under `src/privacyfence/`).
- **Hooks into shared code:** `daemon_main.py` (imports `:97-112` and `:161`, `_configure_upstream_mcp` `:1362`, `build_connectors` `:1764`, the claim-key flags `:1994-2045` and `:2333-2345`); `web/server.py` (token page `:301,331-342`, claim routes and lifespan `:695-734`); `web/mcp_dispatch.py:702-705`; `web/routes_admin.py` (membership actions `:128-136,173-174`, `:329-380`, upstream rows `:383-391`); `web/routes_settings.py:530-536,900-996`; `settings_window_html.py` (token form `:1213-1320`, Admin cards `:2533-2610`, membership page `:2628-2660`); `scoped_tokens.py` (`upstream_audiences`, `GROUP_MEMBERSHIP_TOOL`, `chosen_audiences`); `scoped_token_proposals.py:34-35,157-202`; `runtime_credentials.py:333`; `auto_accept.py:415`; `admin_config.py:157` (`upstream_mcp` default); `org_mode.py:913-917`; `agent_identity.py:288-309` and `web/routes_mcp.py:53,474` (`client_id_scope`, claim-only); `audit_log.py` fields `claim_*`, `upstream_status`, `metadata` (`:439-450`); `tests/conftest.py:37,149,213-220`; `scripts/generate_tools_reference.py`; `deploy/docker/compose.yaml:22,42` and `privacyfence.env.example:79-80`.
- **Must stay (shared):** the OIDC provider and sign-in (`oidc_provider.py`, `org_identity.py`), scoped tokens and n8n credential delivery, `ToolSpec.input_schema_override` (`connector.py:39`) and the verbatim schema path (`web/mcp_tools.py:78`), the ADR 0300 approval machinery (`policy/scopes.py` `TOKEN_ONLY_PREDICATES`, `UPSTREAM_DECLARATIONS`, `upstream.approved_tool`; `gate.py:1782-1804` `is_gated_upstream_tool`, `check_scoped_upstream_approval`; `scoped_tokens.py:601-641`), `policy/registry.py:rebuild_registry` and `policy/propose.py:refresh_all_operations`.
- **On disk after an upgrade:** `org/upstream_claim_keys.json` (Ed25519 seeds, mode 0600) and its lock, `org/group_membership.json`, an `upstream_mcp` section in `admin_config.json` (unknown sections survive load and re-save, so it is harmless; `_fill` raises only for a *default* key that is not an object, which is why the default is removed), the variables `PRIVACYFENCE_UPSTREAM_MCP_FILE` and `PRIVACYFENCE_UPSTREAM_MCP` (read only by the code to be replaced; silently ignored if unread). Old audit lines carry `claim_*`, `upstream_status` and `metadata`; `AuditEntry(**json.loads(line))` (`audit_log.py:787,965`) fails on a field the dataclass no longer has, so those fields stay as legacy.
- **Auto-accept today:** `gated_call` takes its gate from the caller (`gate.py:945`); `review` and `popup` run `_evaluate_auto_accept` (`:702`) against the person's rules (`policy/engine.py:69` `find_matching_rule` matches `operation_key in rule.operations`, then the predicate's selector, `selector.matches(rule.value, ctx)` with `ctx.tool` the exposed tool name, then conditions); a scoped token sees only its own rules (`_rules_in_scope` `:729`) and is denied with no match. The card's Always allow button comes from `policy/propose.proposals_for` (`:583`), which loops the static tuple `PROPOSABLE_SCOPES` (`:300-430`) for scopes of the tool's connector whose verbs include the tool's verb, minus `ProposableScope.excludes`; Settings and token creation read `scope_catalogue()` (`policy/catalogue.py:181`) built from `propose.SCOPES_BY_GROUP`. Upstream tools have no scope, so no button and no rule. `policy/engine.py` skips a rule whose predicate has no selector.
- **Registration today** is a one-shot at startup (`upstream_mcp_registry.register`, `daemon_main.py:1362-1371`) that raises if a name is already present; `rebuild_registry` does `TOOL_REGISTRY.clear()` then `update()` (`policy/registry.py:281`), so a reader in another thread can see it empty; `tests/conftest.py:213-220` resets it.
- **Admin edit path:** `POST /api/admin/{action}` (`web/routes_admin.py:836-841`); `ADMIN_ACTIONS` (`:107`) and `ADMIN_PARAMS` (`:140`) declare an action, `_admin_principal` (`:714`) requires an admin session, step-up with a passkey applies to sensitive actions (`_apply_step_up_gate` is defined in `web/routes_settings.py:434` and called at `routes_admin.py:460`), `admin_config.apply_change` (`admin_config.py:938`) persists. Network-calling actions have no precedent. `settings_window_html.py` has the `data-action` attribute helper (`:364`) with a generic click handler (search `closest('[data-action]')`) and form helpers (`ADMIN_FORMS` `:2341`, `saveAdminForm` `:2688`).
- **MCP SDK** (installed `mcp` 2.3.0, pinned `mcp>=2.2,<3.0.0`, `pyproject.toml:98`): `types.Tool.annotations` is `ToolAnnotations | None` with `read_only_hint`, `destructive_hint` (all `None` when absent; spec defaults readOnly false, destructive true; the SDK calls every hint untrusted); pure OAuth helpers in `mcp.client.auth.utils` (`build_protected_resource_metadata_discovery_urls` `:64`, `build_oauth_authorization_server_metadata_discovery_urls` `:159`, `extract_resource_metadata_from_www_auth` `:52`, `validate_metadata_issuer` `:263`), models in `mcp.shared.auth` (`OAuthToken`, `OAuthMetadata`, `ProtectedResourceMetadata`, `OAuthClientInformationFull`) and `mcp.shared.auth_utils` (`resource_url_from_server_url` `:9`, `check_resource_allowed` `:31`, `calculate_token_expiry` `:68`).
- **Per-person OAuth connectors** (Miro is the template): `web/routes_connect.py` start and callback (`:486,528`, mounted `:817-818`), single-use in-memory `_PendingAuthStore` (`:206`, TTL 600 s), the ADR 0178 session check, `connector_registry.evict` on success; plaintext 0600 token files via `secure_files.atomic_write_json` (`secure_files.py:193`) under `paths.user_dir(principal)/credentials/` (`paths.py:149`); `org_identity.generate_pkce_pair()` (`:222`); the Connect page `_render_connect_page` (`:1095`) and `_flash_html` (`:879`). No disconnect exists for any connector.
- Test patterns: `tests/unit/test_miro_auth.py`, `tests/unit/web/test_routes_connect.py` (`TestMiroOAuth` `:1343`, helpers `:54-69`), `tests/unit/test_systemic_gate_invariants.py` (`TOKEN_WRITE_SITES` `:184`), `tests/unit/test_admin_config.py:881-888` and `tests/unit/web/test_routes_admin.py` (`ACTION_BODIES` `:56`, step-up tests `:232-262`), `tests/unit/test_settings_window_html.py` (`TestOrgAdminSections`).

## 3. Design

### 3.1 What is removed, and what a deployment keeps

Everything listed in section 2 as "to remove" goes, in eight green steps: the feature is first unwired from the daemon, server and Admin (p1), its Admin and token-form UI and the group-membership connector go (p2), the dead core modules are deleted (p3), the scoped-token audience fields go (p4), the claim, key, audience and membership modules are deleted (p5), the claim-only helpers and the verifier go (p6), the reference server, examples, deployment lines and the admin-config default go (p7), and the docs and changelog are cleaned (p8). Two docs stay as short tombstones so the links to them in ADRs and older plans keep working (p8). Nothing is migrated: a declaration file written for the old feature is refused at start with `<source>: version: this release reads version 2 only; version 1 files (tools, identity claim, audiences) are no longer supported, see docs/upstream-mcp.md.` (3.2). At start the server logs one warning for each leftover it finds: `Left over from the removed claim-based upstream feature: org/upstream_claim_keys.json (signing keys; delete it)`, the same for `org/group_membership.json`, and `admin_config.json holds an upstream_mcp section that is no longer used`. The files are never read or deleted by the server. Old ADRs 0285, 0286, 0287, 0288, 0297, 0298, 0299, 0310, 0325 and 0326 are marked `Superseded by ADR 0327`, 0319 `Superseded by ADR 0329`, 0300 `Amended by ADR 0329`; ADR 0323 stays. The audit log's legacy fields stay in `AuditEntry` (never written by the new code) so old lines keep reading; ADR 0327 says so. A scoped token on disk that names the removed tool `privacyfence_update_group_membership`, carries a removed `upstream_audiences` key or names an upstream tool that is not registered loads unchanged and is never rewritten without that part: the unknown tool name stays in the token (so a token is not damaged by loading before the runtime registers the saved lists) and simply never appears in `tools/list` or runs (a test pins each). The unreleased changelog entries that announced the claim feature are deleted with it.

### 3.2 Operator file (version 2)

Read from `PRIVACYFENCE_UPSTREAM_MCP_FILE` (YAML for `.yaml`/`.yml`, else JSON) or inline `PRIVACYFENCE_UPSTREAM_MCP` (YAML), never both, at most 256 KiB, repeated keys refused, exactly as today (copy the loader from `git show f6c95e6b0:src/privacyfence/upstream_mcp_config.py`: `_parse_text`, `_read_file`, `load_declared`, `_UniqueKeyLoader`, `_json_pairs`). Organization servers only. Shape: `version: 2` and `upstreams`, at most 8. Errors read `<source>: upstreams[<i>].<key>: <problem>`.

| Key | Rule | Problem text |
|---|---|---|
| `id` | `[a-z][a-z0-9]{1,15}`, unique, not colliding with a built-in connector name (today's rule, same texts), and not `docs` or `sheets` (the policy maps those operation prefixes to Drive) | as today; for `docs` and `sheets` `upstream id "<id>" collides with the built-in connector "drive"` |
| `label` | 1-60 characters, no control character | as today |
| `url` | `https://`, or `http://` only for `localhost` or a loopback address; no user name or fragment | `must be an https:// address (http:// only for localhost or a loopback address) with no user name or fragment` |
| `allowed_principal_types` | non-empty list from `claude_client`, `n8n_workflow`; default both | as today |
| `oauth` | optional object, every key inside optional | `must be an object` |
| `oauth.scopes` | list of 1-20 strings, each 1-100 characters, no whitespace or control character | `must be a list of 1-20 scopes, each 1-100 characters with no whitespace` |
| `oauth.client_id` | 1-200 characters, no whitespace or control character | `must be 1-200 characters with no whitespace` |
| `oauth.client_secret_env` | name of an environment variable, `[A-Z][A-Z0-9_]{0,63}`, only with `client_id` | `must be an environment variable name in capital letters, digits and underscores`; `needs client_id` |
| `oauth.issuer` | `https://` address with no user name, query or fragment (a path is allowed; `http://` only for a loopback host) | `must be an https:// address with no user name, query or fragment` |

Any other key, `tools`, `audience`, `identity_claim` and the top-level `claim` and `membership` included, is `<path>: is not a known key`. A `version` other than 2 is `<source>: version: this release reads version 2 only; version 1 files (tools, identity claim, audiences) are no longer supported, see docs/upstream-mcp.md.` for 1 and `<source>: version: must be 2` otherwise. `UpstreamServer` (frozen): `id`, `label`, `url`, `oauth: UpstreamOAuthSettings` (frozen: `scopes: tuple[str, ...] = ()`, `client_id: str | None = None`, `client_secret_env: str | None = None`, `issuer: str | None = None`), `allowed_principal_types: tuple[str, ...]`.

### 3.3 Discovery (`upstream_oauth_discovery.discover`)

`async def discover(upstream: UpstreamServer, *, transport: httpx2.AsyncBaseTransport | None = None) -> OAuthEndpoints`; `OAuthEndpoints` is a frozen dataclass: `issuer: str`, `authorization_endpoint: str`, `token_endpoint: str`, `registration_endpoint: str | None`, `resource: str`, `scope: str | None`, `token_auth_methods: tuple[str, ...]`, `iss_supported: bool`, `loopback: bool`.

**The URL rule.** `_checked_url(url: str, *, host: str | None, loopback: bool) -> None` raises `UpstreamOAuthError("invalid_metadata")` unless: the scheme is `https` (`http` only when `loopback`); there is no user name, password or fragment; and, unless `loopback`, the host is not an IP literal in a private, loopback, link-local, multicast, reserved or unspecified range, has at least two labels and does not end in `.local`, `.internal`, `.home.arpa` or `.lan`. When `host` is not `None` the URL's lower-cased `host[:port]` must equal `host` exactly. `loopback` is true when `upstream.url` is `http://` (the config limits that to a loopback host), and it relaxes the rule **only for a URL whose own host is `localhost` or a loopback IP literal**: such a URL may be `http` and may be a loopback address; any other host in the metadata still has to be https and public, so a local upstream cannot point the server at `169.254.169.254`.

**DNS.** `async _resolve(host: str) -> list[str]` runs `socket.getaddrinfo(host, None)` through `asyncio.to_thread` and returns the address strings (tests patch it). `_require_public(host, loopback)` is skipped only when `loopback` is true and the host is `localhost` or a loopback IP literal; otherwise a resolution error raises `UpstreamOAuthError("unreachable")` and any address for which `ipaddress.ip_address(...).is_global` is false (zone id after `%` removed) raises `invalid_metadata`. It is applied once, to the issuer's host (step 4); the endpoints are bound to that host. The connection resolves the name again later, so a name that answers differently the second time is a residual risk, recorded in ADR 0328.

1. Send one unauthenticated `POST` of `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"PrivacyFence","version":"0"}}}` to `upstream.url` with `Accept: application/json, text/event-stream`, redirects off, the body read through the 64 KiB cap; only the status and the response headers are used. A transport error or timeout raises `UpstreamOAuthError("unreachable")`. On a 401 read `resource_metadata` from `WWW-Authenticate` with `extract_resource_metadata_from_www_auth` (it takes the response object, so this one request is made with `httpx2` directly through the same capped reader); otherwise there is no hint.
2. Candidate metadata URLs come from `build_protected_resource_metadata_discovery_urls(hint, upstream.url)`. Each, the hint included, must pass `_checked_url(url, host=<the upstream's host[:port]>, loopback=loopback)`; one that does not is skipped (a hint on another host is thereby never followed). Fetch the survivors in order with `GET` until one answers 200 with a body that `ProtectedResourceMetadata.model_validate_json` accepts (the SDK model requires `resource` and a non-empty `authorization_servers`, so a document without them fails here). None: `UpstreamOAuthError("invalid_metadata")`.
3. Check the resource: `check_resource_allowed(resource_url_from_server_url(upstream.url), str(prm.resource))`, else refuse.
4. The authorization server is `upstream.oauth.issuer` when pinned (it must equal one of `prm.authorization_servers`, comparing `str(value).rstrip("/")` of both sides, else refuse), otherwise `str(prm.authorization_servers[0])`. Call `_checked_url(issuer, host=None, loopback=loopback)` and `_require_public(issuer_host, loopback)`; `issuer_netloc` is its lower-cased `host[:port]`.
5. Fetch `build_oauth_authorization_server_metadata_discovery_urls(issuer, upstream.url)` in order (each must pass `_checked_url(url, host=issuer_netloc, loopback=loopback)`, else skipped) until one parses as `OAuthMetadata`; call `validate_metadata_issuer(metadata, issuer)` and turn an `OAuthFlowError` from it into `UpstreamOAuthError("invalid_metadata")`. None found: refuse.
6. Require `authorization_endpoint` and `token_endpoint`; each, and `registration_endpoint` when present, must pass `_checked_url(url, host=issuer_netloc, loopback=loopback)`. Require `"S256"` in `code_challenge_methods_supported` (an absent list is a refusal). When `registration_endpoint` is absent and `upstream.oauth.client_id` is not declared, `discover` still returns (`RegistrationStore` refuses, 3.4).
7. `scope` is `" ".join(upstream.oauth.scopes)` when declared, else `" ".join(prm.scopes_supported)` when that list is non-empty, else `None`. Asking for every listed scope is the least-private default; the runtime logs `Upstream <id> declares no oauth.scopes; it will ask for every scope the server lists.` at start for an upstream that declares none, and the docs tell operators to declare them.
8. `token_auth_methods` is `tuple(metadata.token_endpoint_auth_methods_supported or ("client_secret_basic",))`; `iss_supported` is `bool(metadata.authorization_response_iss_parameter_supported)`.
9. Never used: the SDK fallbacks to `<origin>/register`, `<origin>/token` and the legacy origin well-known URL.

### 3.4 Client registration (`RegistrationStore`)

`RegistrationStore(directory: Path, *, transport=None)`; the runtime passes `paths.org_dir() / "upstream_oauth"`. It holds one `asyncio.Lock` per upstream id (single flight).

- `async ensure_client(upstream, endpoints, redirect_uri) -> OAuthClient` returns, in order: (a) with `upstream.oauth.client_id` declared: that id, with the secret read from the environment variable `client_secret_env` at call time (a declared name whose variable is empty raises `UpstreamOAuthError("not_configured")`); `auth_method` is `none` without a secret, else `client_secret_post` when `endpoints.token_auth_methods` lists it, else `client_secret_basic`; nothing is written. (b) A cached registration from `<directory>/<id>.json` whose `issuer` and `redirect_uri` equal the current ones and whose `client_secret_expires_at` is absent, 0 or more than 60 seconds ahead. (c) A new registration: `endpoints.registration_endpoint` absent raises `UpstreamOAuthError("registration_failed")`; otherwise `POST` JSON `{"client_name": "PrivacyFence", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": <m>, "scope": <endpoints.scope, omitted when None>}` where `<m>` is `none` if `endpoints.token_auth_methods` lists it, else `client_secret_post` if listed, else `client_secret_basic`. Parse the answer as `OAuthClientInformationFull`; require a `client_id`, and when it lists `redirect_uris` require ours among them; any failure raises `UpstreamOAuthError("registration_failed")` without the body. Write `{"version": 1, "upstream_id", "issuer", "redirect_uri", "client_id", "client_secret", "client_secret_expires_at", "auth_method", "registered_at"}` with `secure_files.atomic_write_json` (directory made with `secure_files.secure_mkdir`).
- `client_for(upstream_id: str) -> OAuthClient` (no network) returns the declared client (secret from the environment) or the cached registration, else raises `UpstreamOAuthError("not_configured")`. The token provider uses it for refreshes.
- `forget(upstream_id)` removes the cache file; the runtime passes it as the token provider's `on_invalid_client` (3.5), so a vendor that dropped a dynamically registered client makes the next Connect register a new one.
- The redirect URI is `<issuer_url without trailing slash>/upstream-mcp/<id>/callback`; a changed server address therefore re-registers on the next Connect. There is no relay: the redirect goes straight to the server (ADR 0259's relay serves only built-in apps).

### 3.5 Tokens (`upstream_oauth_tokens`)

- `UpstreamOAuthError(kind)` with kinds `not_connected`, `auth_expired`, `unreachable`, `invalid_metadata`, `not_configured`, `registration_failed`, `insufficient_scope`; `str(exc)` is the kind only.
- `async http_json(method: str, url: str, *, transport=None, headers=None, json_body=None, form=None, auth=None) -> tuple[int, dict[str, Any] | None, httpx2.Headers]`: one request through `httpx2.AsyncClient(transport=transport, timeout=httpx2.Timeout(10.0))`, redirects off, the body read in chunks and refused above 64 KiB; returns the status, the body parsed as a JSON object (`None` when it is not valid JSON or not an object) and the headers; a transport error, timeout or oversize body raises `UpstreamOAuthError("unreachable")`. Every request of the discovery, registration and token code goes through it.
- `TokenRecord` (frozen): `upstream_id`, `access_token`, `refresh_token: str | None`, `expires_at: float | None`, `scope: str`, `issuer`, `client_id`, `auth_method`, `token_endpoint`, `resource: str | None`, `connected_at: float`. The file is `paths.user_dir(principal) / "credentials" / "upstream_mcp" / "<id>.json"` (`token_path(principal, upstream_id)`), `{"version": 1, ...}`, written with `secure_files.atomic_write_json` after `secure_mkdir`, 0600, plaintext like every other third-party token (ADR 0072 rejects encryption under a key the server holds). `token_paths(upstream_id)` lists `paths.data_dir() / "users" / "*" / "credentials" / "upstream_mcp" / "<id>.json"`, newest modification first. `load_token(path)` returns `None` when the file is absent or malformed; `save_token`, `delete_token`.
- `OAuthClient` (frozen): `client_id`, `client_secret: str | None`, `auth_method`.
- `build_authorize_url(authorization_endpoint, client_id, redirect_uri, state, code_challenge, scope, resource) -> str`: query `response_type=code`, `client_id`, `redirect_uri`, `state`, `code_challenge`, `code_challenge_method=S256`, plus `scope` and `resource` when not `None`.
- `async exchange_code(*, token_endpoint, client, code, redirect_uri, code_verifier, resource, upstream_id, issuer, transport=None) -> TokenRecord`: form `POST` with `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `code_verifier` and `resource` when set; client authentication by `client.auth_method` (`client_secret_post`: `client_secret` in the form; `client_secret_basic`: `httpx2.BasicAuth(quote(id, safe=""), quote(secret, safe=""))`; `none`: nothing). The answer must parse as `OAuthToken` (the SDK model already requires `Bearer`, ignoring case) and carry a non-empty `access_token`, else `unreachable`; `expires_at = calculate_token_expiry(expires_in)` (a missing `expires_in` means `None`: no proactive refresh). A 400 or 401 raises `UpstreamOAuthError("auth_expired")`, any other failure `unreachable`, never quoting the body.
- `UpstreamTokenProvider(client_source: Callable[[str], OAuthClient], *, on_invalid_client: Callable[[str], None] | None = None, clock=time.time, transport=None)`; `async bearer(path: Path, force_refresh: bool = False) -> str`: load the file (absent: `not_connected`); when `force_refresh` or `expires_at` is within 60 seconds, take the per-path `asyncio.Lock`, re-read the file (another call may have refreshed), and if still stale and there is a `refresh_token`, `POST` `grant_type=refresh_token`, `refresh_token`, `client_id` and `resource` (when set) to the stored `token_endpoint` with the client from `client_source(record.upstream_id)`; a client whose `client_id` differs from the record's raises `auth_expired` after deleting the file; the new record keeps the old refresh token when none is returned and is saved. A 400 or 401 answer deletes the file and raises `auth_expired`, after calling `on_invalid_client(record.upstream_id)` when the answer's JSON `error` is `invalid_client`; any other failure raises `unreachable` and keeps the file. A stale token with no refresh token raises `auth_expired` after deleting the file.
- Log lines in the token and discovery modules name the upstream id and an error kind only; no exception, log line or message holds a response body, a token, a code or a secret.
- No module-level mutable state in either module: locks and stores are instance attributes created by the runtime and the routes.
- Rejected: the SDK's `OAuthClientProvider`. It starts an interactive authorization from inside a request, and it keeps no authorization-server metadata, so a refresh after a restart would go to `<origin>/token`, wrong whenever the authorization server is on another host. The pure helpers are used instead, and a unit test per module imports every helper name it uses, so an SDK bump within `mcp<3` that removes one fails loudly.

### 3.6 The tool list: refresh, defaults, hold, save (`upstream_tools.py`)

**Gates.** A tool's *configured gate* is `review`, `popup` or `auto`. Its *class default* is `review` when the server lists `readOnlyHint=true` and not `destructiveHint=true`, otherwise `popup`. The gate a person actually meets (`person_gate`) is the configured gate when it is `review` or `popup`, else the class default: **`auto` is never enforced**, it only means that a person may make an Always allow rule for that tool (3.7). A configured gate of `review` on a tool whose class default is `popup` is allowed (the administrator's choice; the list marks it "runs before you see it").

**Verb.** `readOnlyHint=true` and not `destructiveHint=true`: `read`; else `destructiveHint=false`: `update`; else `delete` (the spec default of an absent hint is destructive). The policy verb decides which scoped-token scopes and rules cover the tool.

**Module `src/privacyfence/upstream_tools.py` (new, pure plus one store).** Frozen dataclasses `ToolEntry(name, description, input_schema, read_only: bool | None, destructive: bool | None)` and `SavedTool(name, description, input_schema, read_only, destructive, gate, verb, digest)`; `classify(read_only, destructive) -> tuple[str, str]` (class default gate, verb); `person_gate(configured: str, class_default: str) -> str`; `unsupported_reason(upstream_id, entry) -> str | None` (checked in this order, each reason a fixed sentence: a name that does not match `[A-Za-z0-9_-]{1,48}`: `its name is not 1-48 letters, digits, underscores or hyphens`; `<id>_<name>` over 64 characters: `its exposed name is longer than 64 characters`; a schema that is not an object schema (`type` not `object`, or `properties` not an object, or `required` not a list): `its input schema is not an object schema`; a `reason` property: `its input schema already has a reason property`; more than 32 KiB of JSON: `its input schema is larger than 32 KB`; any `$ref`, `$defs` or `definitions` key at any depth: `its input schema uses $ref, $defs or definitions`); `tool_digest(entry) -> str` (first 32 hex characters of the SHA-256 of the canonical JSON of name, description, input schema and both hints); `declaration_digest(upstream_url, operation, verb, configured_gate, digest) -> str` (first 32 hex characters of the SHA-256 of the canonical JSON `{"gate", "operation", "tool", "url", "verb"}`; it is what an ADR 0300 approval is pinned to, so a gate, verb, URL or tool change voids it); `diff(saved, fetched) -> ToolDiff(new, changed, removed, unchanged, unsupported)` (names, by digest). At most 50 supported tools are kept per server; the rest are `unsupported` with the reason `more than 50 tools`. `ToolStore(directory)` keeps `<directory>/<id>.json` (`{"version": 1, "upstream_id", "url", "saved_at", "tools": [...]}`, written with `secure_files.atomic_write_json`, 0600); `load(upstream_id) -> SavedList | None` (absent or malformed: `None`; `SavedList` has `upstream_id`, `url`, `saved_at`, `tools`), `save(upstream_id, url, tools, now)`. The runtime passes `paths.org_dir() / "upstream_tools"`.

**Flow (runtime, p15; actions, p16).**
1. An administrator who has connected presses **Refresh tools**: the runtime lists the server's tools with *that administrator's* token (`client.list_tools(bearer=...)`), classifies and diffs against the saved list, and keeps the result in memory as the *pending list* (`fetched_at`, per tool the entry, class default, verb, state `new`/`changed`/`unchanged`/`unsupported`, and the removed names). Nothing is exposed or changed yet. If the administrator has not connected: `Connect <label> on the Connect page (/connect) first, then refresh.`
2. The administrator reviews the pending list (defaults are the class defaults for new and changed tools, the saved gate for known unchanged ones) and presses **Save**: the pending list becomes the saved list with the chosen gates, is written to the store, registered into the policy tables (3.7) and the AI clients are told the tool list changed. Tools in the pending list but not chosen keep their default. Removed tools disappear. A tool that is `unsupported` is never saved.
3. **Hold:** until Save, AI clients keep seeing the previously saved list, whatever the server now lists or says in its descriptions. This is the rug-pull guard of ADR 0285, moved from "once at startup" to "until an administrator saves".
4. A tool the pending list marks `changed` (its digest differs from the saved one) is offered with its class default gate, not its saved gate: an `auto` ceiling does not survive a change the administrator has not looked at, and the people's Always allow rules for it go inert until `auto` is chosen again.
5. **Save is bound to what was reviewed:** `save_upstream_tools` carries the `fetched_at` of the pending list the administrator saw and is refused when it differs from the current pending list's (another administrator refreshed in between).
6. Gate changes on saved tools without a refresh are a separate **Save gates** action (`set_upstream_tool_gates`).
7. At start the saved lists are registered with no network call; a server with no saved list exposes no tools (status `waiting_for_admin`). The saved file records the upstream's `url`; a saved list made for another address is ignored with the warning `Upstream <id>: the saved tool list was made for another address and is ignored; refresh the tools.`, so a person's rule never outlives the server it was made for.

### 3.7 Live registration, per-tool rules and the `auto` ceiling (p13, p15)

`upstream_mcp_registry.UpstreamRegistry` (one process-wide instance from `get_registry()`, created lazily; `reset()` for tests) holds an `RLock` and, per upstream, the names, operations and scopes it added. `apply(upstream: UpstreamServer, saved: tuple[SavedTool, ...]) -> None` replaces everything the upstream previously registered and registers the new list. For each saved tool, with `name = f"{upstream.id}_{tool}"` and `operation = f"{upstream.id}.{tool}"`: `auto_accept.TOOL_TO_GATE[name] = person_gate(...)` (so it is `review` or `popup`, **never `auto`**); `auto_accept.TOOL_TO_OPERATION[name] = operation`; `policy.registry.TOOL_TO_VERB[name] = Verb(verb)`; for a person gate of `popup`, `write_effects.EFFECT_BY_TOOL[name] = "Calls <tool> on <label>; the server may change data."` (fixed template, no server text); `gate._TOOL_LAYOUT[name] = WIDE`; `policy.scopes.UPSTREAM_DECLARATIONS[operation] = declaration_digest(...)`; and when the configured gate is `auto`, `policy.scopes.UPSTREAM_AUTO_TOOLS` (new `set[str]` of exposed names) gains `name`. Then `policy.registry.rebuild_registry()` and `policy.propose.refresh_all_operations()` run, then the per-tool scopes below and `describe.set_connector_labels`. `rebuild_registry` is changed to build the new mapping first, `TOOL_REGISTRY.update(new)`, then delete the keys not in `new`, so no reader ever sees it empty. `clear(upstream_id)` and `unregister_all()` undo it (the latter for tests); a name already in `TOOL_TO_GATE` that this registry did not add raises `ConfigurationError`.

**Per-tool Always allow.** One new predicate `upstream.always_allow` in `policy.scopes.NEW_SCOPE_SELECTORS`: `ScopeSelector(predicate="upstream.always_allow", scope_type="upstream.always_allow", kind=ScopeKind.ATTRIBUTE, resolves_from=ResolvesFrom.ARGS, matches=_upstream_always_allow_matches)`, where `_upstream_always_allow_matches(value, ctx)` is true when `value` is a string in `UPSTREAM_AUTO_TOOLS` and `ctx.tool in ("", value)` (`ctx.tool` is the exposed tool name; the policy preflight builds the context with an empty tool, and the rule's own `operations` already confine it to one tool). The rule's **value is the exposed tool name**, so every tool has its own rule and its own row id. The ceiling is enforced at match time: a stored rule for a tool the administrator later lowers (or whose changed digest reset it) stays stored and matches nothing. `policy.propose._candidate_value` currently looks only in `scopes.SCOPE_SELECTORS`; it is changed to look in `SCOPE_SELECTORS` and then `NEW_SCOPE_SELECTORS` (as `policy.engine._selector_for` already does), or no button would ever render. `ProposableScope` gains `fixed_value: Any = NO_VALUE`; `scope_needs_value` is false for a scope with a fixed value; `rules_for_scope_group` uses it instead of a typed value. For every saved tool whose configured gate is `auto`, `policy.propose` gains one `ProposableScope(id=f"upstream.always_allow/{name}", predicate="upstream.always_allow", scope_type="upstream.always_allow", connector=upstream.id, verbs=frozenset({verb}), value_of=<returns name>, hint=tool, widening_group=f"upstream.always_allow/{name}", excludes=<every other operation of that connector>, widenable=False, fixed_value=name)`, added through `propose.set_upstream_scopes(scopes)` that rebinds `PROPOSABLE_SCOPES` to the static scopes plus these and updates `_SCOPES_BY_GROUP` and `SCOPES_BY_GROUP` in place (new keys first, then stale keys deleted). Because the scope is in `PROPOSABLE_SCOPES`, `proposals_for` offers the card's Always allow button for that tool only; the rule it writes is `PolicyRule(id="upstream.always_allow", predicate="upstream.always_allow", value=<name>, operations={operation})`; `policy.describe.connector_label` learns an upstream's label through `describe.set_connector_labels(mapping)`, so the Settings rule picker and token creation list the scope as `<Label> — <Tool>` (the catalogue capitalises the hint). **Stored rows:** `policy.scope_rows.scope_of_rule` returns `None` for the predicate `upstream.always_allow`, so the rule is always kept as a *custom* row (a scoped row would be dropped by `row_from_dict` when its scope group disappears, and several auto tools of one upstream would be ambiguous); `policy.outdated.missing_operations` skips this predicate (otherwise it would report every other auto tool as missing and "fill missing" would widen the rule).

**Scoped tokens.** Every upstream tool now has a person gate of `review` or `popup`, so `gate.is_gated_upstream_tool` is true for all of them and a token that lists one needs an approval. `scoped_tokens._checked_upstream_approvals` is changed so that a listed gated upstream tool is accepted when **either** an administrator's tick (as today, ADR 0300, digest from `declaration_digest`) **or** one of the token's own rules covers it: a rule with predicate `upstream.always_allow` whose value is the tool's exposed name, whose operations contain the tool's operation, and whose tool is in `UPSTREAM_AUTO_TOOLS`; any creator may add such a rule through the per-tool scope of the rule picker. The refusal text becomes `<name> changes data or releases results unseen; an administrator must approve it on the New token form, or the token needs an Always allow rule for it.` The token page offers an upstream tool in the main tool list when it is in `UPSTREAM_AUTO_TOOLS` (the rule can cover it) and otherwise only in the administrator's tick fieldset. The preflight and `privacyfence_check_policy` need no change: the empty `ctx.tool` is handled by the selector. Existing approvals of the old feature are void (their digests were of the old declarations).

### 3.8 Client, connector and caller policy (p14)

- `UpstreamMcpClient(upstream, *, transport=None)`: one Streamable HTTP session per call (`list_tools(self, *, bearer)` and `call_tool(self, tool, arguments, *, bearer)`, `BearerSource = Callable[[bool], Awaitable[str]]`, the argument being `force_refresh`). It gets the token with `await bearer(False)` (an `UpstreamOAuthError` becomes `UpstreamMcpClientError` of kind `not_connected`, `auth_expired`, `insufficient_scope` or `unreachable`), sends it through `BearerAuth(token, origin_of(upstream.url))` and tracks a phase flag: a 401 seen during `session.initialize()` gets exactly one retry with `await bearer(True)` on a fresh session; a 401 after `initialize` raises `auth_expired` and the call is never sent again; a 403 with `insufficient_scope` raises `insufficient_scope`; everything else is `unreachable`; a result over 1,000,000 characters is `too_large`, an error result is `tool_error` with the server's text. `BearerAuth(token, origin)` (an `httpx2.Auth` in the same module): sets `Authorization: Bearer <token>` only on a request whose origin equals `origin`; records `status` and `insufficient_scope` after each response. **No claim header is ever sent.** Copy the call plumbing from `git show f6c95e6b0:src/privacyfence/upstream_mcp_client.py` (`_run`, `call_tool`, `CallResult`, `MAX_RESULT_CHARS`, the schema pass-through in `UpstreamToolCatalog._spec` including the added required `reason` property and the 700-character description cut).
- `UpstreamMcpConnector(upstream=None, catalog=None, client=None, *, admit=None, bearer=None)` (every argument optional: the connector catalogue builds `cls(None)` and `cls(MagicMock())`; without an upstream its name is `upstream_mcp` and `tool_specs()` is empty) (copy the structure of `git show f6c95e6b0:src/privacyfence/connectors/upstream_mcp.py`): tool specs come from the saved list; `call` looks the tool up by exposed name, runs `admit()` (any `CallerNotAllowedError` becomes `UpstreamCallRefusedError` `PrivacyFence does not allow this caller to use the <label> server.`), then goes through `gated_call` with gate `person_gate` (**never** an unconditional path: a matching rule is what makes a `review` or `popup` call run without a card). `review` forwards, then shows the result; `popup` asks, then forwards. Messages (`UpstreamUnavailableError`): `not_connected`: `<label> is not connected. Connect <label> on the Connect page (/connect).`; `auth_expired`: `The <label> sign-in has expired or was revoked. Connect <label> on the Connect page (/connect) to sign in again.`; `insufficient_scope`: `<label> needs more access than PrivacyFence was granted. Connect <label> again on the Connect page (/connect).`; `unreachable`: `The <label> server could not be reached.`; `too_large`: `The <label> result is larger than 1 MB; ask for less.`; a server error result is `UpstreamToolError` `The <label> server returned an error: <text cut to 500 characters, control characters removed>`. The audit entry per forwarded call (`decision: upstream_forwarded`, `metadata: {"upstream", "gate", "auth": "oauth"}`, `upstream_status` the kind) holds no argument value, token or server text.
- `CallerNotAllowedError(Exception)` (with `.principal_type`) is defined in `upstream_mcp_client.py`; `upstream_mcp_runtime.admit_caller(upstream)` raises `CallerNotAllowedError(principal_type)` when the caller's type (`n8n_workflow` when `current_token_scope()` is set, else `claude_client`) is not in `upstream.allowed_principal_types`.

### 3.9 Runtime, status and the Connect flow (p15, p17)

- `upstream_mcp_runtime.configure(*, upstreams, tool_store, registrations_dir, transport=None)` builds, per upstream, a `RegistrationStore`, an `UpstreamTokenProvider(client_source=registrations.client_for, on_invalid_client=registrations.forget)`, an `UpstreamMcpClient`, loads the saved list, calls `upstream_mcp_registry.get_registry().apply`, and raises `org_mode.ConfigurationError` `Upstream "<id>": oauth.client_secret_env names <NAME>, which is not set.` for an empty secret variable. It logs the no-scopes warning of 3.3; the leftover warnings of 3.1 are logged by the daemon's `_configure_upstream_mcp` on every start, before the declaration is read, so a server whose variable was removed or still points at a version 1 file also logs them. `set_tools_changed_callback(cb)` is set by `web/server.py` to `mcp_dispatcher.notify_tools_changed`. There is no startup network call and no lifespan.
- `connectors_for_principal()` returns a connector for an upstream only when the saved list is non-empty and the current principal has a token file.
- `status()` rows (`privacyfence_status`): `id`, `label`, `tools_exposed`, `state` (`waiting_for_admin` when there is no saved list, with `admin_hint` `An administrator has to refresh the tool list on the Admin page.`; else `ready`), `connected` (the caller has a token file) and, when not connected, `connect_hint` `Connect <label> on the Connect page (/connect).`.
- `admin_rows()` rows (Admin page, 3.10): `id`, `label`, `url` (scheme and host only, then `/…` when there is a path or query), `issuer_host`, `registration` (`declared` or `dynamic`), `scopes`, `connected_people` (count of token files, never who), `admin_connected`, `allowed_principal_types`, `saved_at`, `tools` (saved: `name`, `exposed_name`, `verb`, `read_only`, `default_gate`, `gate`, `description` cut to 200 characters with control characters removed), `pending` (`None`, or `fetched_at`, `new`, `changed`, `removed`, `unsupported` with reasons, and per tool `default_gate`, `verb`, `state`, `description`).
- **Connect flow** (`web/routes_upstream_oauth.py`, `build_routes(*, sessions, connector_registry, issuer_url, transport=None)`): `GET /upstream-mcp/{id}/connect` (no session: 302 to `/login?next=/connect`; unknown id: 404 `Not found`; else `discover` → `RegistrationStore.ensure_client` → `generate_pkce_pair()` and `state = secrets.token_urlsafe(32)` → pending store → 302 to `build_authorize_url(...)` with `Cache-Control: no-store`; any failure logs `Upstream <id> connect failed: <kind>` and 302s to `/connect?upstream_error=<id>`); `GET /upstream-mcp/{id}/callback` (`pending.pop(state)`; missing, expired or another upstream's: 400 `Invalid or expired sign-in attempt`; the signed-in principal must equal the pending one (ADR 0178); a provider `error` (logged as its code only, kept to `[A-Za-z0-9_.-]`, 40 characters), a missing `code`, a present `iss` that differs from the pending issuer, or a missing `iss` when `iss_supported` (RFC 9207) is the same redirect; then `exchange_code`, `save_token`, `connector_registry.evict(principal.id)` and 302 to `/connect?upstream_connected=<id>`). `PendingUpstreamAuthStore(ttl_seconds=600, max_entries=1000, clock=time.time)` (in that module): `put(state, PendingUpstreamAuth)` and a single-use `pop(state)` that returns `None` after the TTL, dropping the oldest entry beyond `max_entries`; `PendingUpstreamAuth` (frozen): `principal_id`, `upstream_id`, `code_verifier`, `redirect_uri`, `issuer`, `client: OAuthClient`, `token_endpoint`, `resource`, `iss_required`. The state, code and any body are never logged. `connect_section_html(principal, flash_connected_id, flash_error_id) -> tuple[str, str]` (flash HTML, row HTML) is called from `routes_connect._render_connect_page`, which reads the query parameters `upstream_connected` and `upstream_error` (a value that is not a declared upstream id is ignored); each row is built like `_service_row_html` (the label HTML-escaped; badge `Connected` or `Not connected`; link `Connect` or `Reconnect` to `/upstream-mcp/<id>/connect`); flash texts `<label> connected.` and `Could not connect <label> -- sign-in was declined, or the server could not be reached.`

### 3.10 Admin page (p16, p18)

Three actions in `routes_admin.py`, handled by `upstream_mcp_runtime` and not by `admin_config.apply_change` (they persist to `org/upstream_tools/`, not `admin_config.json`): `refresh_upstream_tools` (params `upstream_id`; not sensitive), `save_upstream_tools` and `set_upstream_tool_gates` (params `upstream_id` and `gates`, an object of at most 50 `{<tool name>: review|popup|auto}`, and for `save_upstream_tools` also `fetched_at`; both sensitive, so they need the admin passkey step-up like every sensitive action). Errors: an unknown upstream id is 400 `Unknown upstream.`; `save_upstream_tools` without a pending list is 409 `Refresh the tool list first.` and with a `fetched_at` that is not the pending list's is 409 `The tool list changed since you refreshed it. Refresh again.`; a gate outside the three values is 400 `Gate must be review, popup or auto.`; `set_upstream_tool_gates` for a tool that is not saved is 400 `That tool is not in the saved list.`; the refresh failures use the connector messages of 3.8 (`not_connected` reads `Connect <label> on the Connect page (/connect) first, then refresh.`). Each action writes one audit row (decision `upstream_tools_refreshed`, `upstream_tools_saved` or `upstream_tool_gates_changed`; the summary holds the upstream id and counts only, never a tool description). The admin state gains `upstream_servers` (rows of `admin_rows()`, replacing the read-only listing of ADR 0319).

UI (`settings_window_html.py`, `renderUpstreams`): one card per upstream with the address, `Sign-in: OAuth at <issuer_host>, <n> person(s) connected, client <declared|dynamic>.`, the state (`Waiting for an administrator to refresh the tools. AI clients see none of its tools until you save a list.`), a **Refresh tools** button (`data-action="refresh_upstream_tools"`; disabled with `Connect <label> on the Connect page first.` when `admin_connected` is false), and a table of tools: name, a badge (`read`, `write`, `destructive` from the verb), a gate `<select>` with `review`, `popup`, `auto` (the class default marked `default`), and for a pending tool a badge `new` or `changed` and the line `Not exposed until you save.`. Under the table: `auto lets each person create an Always allow rule for the tool; until they do, it still asks (review or popup as shown).` and a **Save** button (`save_upstream_tools`, with the pending list's `fetched_at`, when a pending list exists, else `set_upstream_tool_gates`). A `review` choice on a non-read tool shows `Runs before the person sees anything.` Descriptions are shown escaped.

### 3.11 Tests' reference server and rejected alternatives

**Reference upstream with OAuth (p19).** New `scripts/upstream_mcp_oauth_reference_server.py`, a test aid (not production): `build_app(*, base_url: str, accounts: dict[str, str] | None = None, access_token_seconds: int = 3600) -> Starlette`. It serves `/.well-known/oauth-protected-resource` (resource `<base_url>/mcp`, `authorization_servers` `[<base_url>]`, `scopes_supported` `["mcp:read"]`), `/.well-known/oauth-authorization-server` (issuer, endpoints, `code_challenge_methods_supported` `["S256"]`, `token_endpoint_auth_methods_supported` `["none"]`, `authorization_response_iss_parameter_supported` true), `POST /register`, `GET /authorize` (checks `response_type=code`, a registered `redirect_uri`, S256; auto-approves and 302s to `redirect_uri` with `code`, `state` and `iss`), `POST /token` (`authorization_code` with the PKCE check; `refresh_token` with rotation) and `/mcp` (Streamable HTTP, built like the existing reference server's `build_app`; 401 with `WWW-Authenticate: Bearer resource_metadata="<base_url>/.well-known/oauth-protected-resource"` without a valid bearer). **Accounts:** `/authorize` takes an optional `login_hint` query parameter; the account is `accounts[hint]` when the hint is a key of `accounts`, else the hint itself, else `"alice"`; it is stored with the code and then the tokens. Tools: `whoami` (returns the account of the token; annotated `readOnlyHint=true`), `echo` (returns its `text` argument; annotated `readOnlyHint=true`), `rename_thing` (no annotation; returns `renamed`) and `wipe_thing` (`destructiveHint=true`; returns `wiped`), and a settable `app.state.extra_tools` list for the refresh tests. `app.state.revoke_all()` forgets every access and refresh token and nothing else.

**Rejected alternatives (for the ADRs).**
- Keeping the claim feature beside the OAuth one: owner decision, dropped.
- The SDK's `OAuthClientProvider` (3.5).
- A Client ID Metadata Document served by PrivacyFence: PrivacyFence cannot check that the vendor can fetch it; owner decision, left out; the registration cache records the method so adding it needs no migration.
- Taking gates from the server's annotations as the final word (ADR 0285's rejection stands): annotations only seed a default the administrator reviews, and only `readOnlyHint=true` earns `review`.
- Exposing newly listed tools at once (owner decision: held until saved).
- An unconditional `auto` path: owner decision, `auto` is a ceiling and a person's rule is what removes the card.
- One Always allow rule per server: owner decision, per tool.
- Encrypting tokens at rest or sealing them to the bearer: ADR 0072 rejects a key stored beside the data, and refresh and scoped-token calls run with nobody present.
- A Disconnect button: owner decision, left out; Reconnect overwrites the file and a person leaves by removing the grant at the vendor.
- Editing the declared tools in the operator file: dropped for the Admin page, so a restart is no longer needed to add a tool.

## 4. ADRs

- **0327** Hosted MCP servers sign in each person with their own OAuth, and the claim-based upstream feature is removed: the version 2 operator file, no claim ever sent, `allowed_principal_types` enforced for every call, a scoped token acting through its creator's connection, what the removal leaves on disk and in the audit log. Supersedes 0285, 0286, 0287, 0288, 0297, 0298, 0299, 0310, 0325, 0326.
- **0328** Discovery, client registration and token storage: the metadata chain, the host, address and DNS rules and their rebinding residual, declared client else dynamic registration (the metadata-document method deferred), the registration cache under the org directory, plaintext per-person tokens at 0600, the SDK helpers used and why not `OAuthClientProvider`, no Disconnect. Relates to 0072, 0178, 0259.
- **0329** The administrator manages each server's tools: Refresh with the administrator's own connection, defaults from untrusted annotations (only `readOnlyHint=true` earns `review`), new or changed tools held until Save, `review`/`popup`/`auto` with `auto` as a ceiling, live registration of the policy tables, the per-tool Always allow rule and the `upstream.always_allow` predicate, the digest of ADR 0300 approvals and the token-rule route, that Refresh reads a server-wide list with the administrator's own connection, the Admin page and `privacyfence_status` fields. Supersedes 0319; amends 0300.

## 5. Manual steps

### Before implementation

None. Nothing in the phases needs a third-party account: they run against the reference server of 3.11.

### After implementation

**1. Connect a real Canva account on the QA server (about 25 minutes).** Needs a QA organization server reachable on an https address running this branch (see [The QA server](https://github.com/privacyfence/privacyfence/blob/feature/oauth-upstream-mcp/docs/connector-qa.md#the-qa-server)), an administrator account on it with a passkey, and a Canva account that holds nothing real.
1. On the QA server declare the upstream as in [Upstream MCP servers](https://github.com/privacyfence/privacyfence/blob/feature/oauth-upstream-mcp/docs/upstream-mcp.md): `id: canva`, `label: Canva`, `url: https://mcp.canva.com/mcp`, version 2. Restart. `privacyfence_status` should show `waiting_for_admin`.
2. Open `https://<QA server>/connect`, find the **Canva** row and click **Connect**. You should land on Canva's sign-in or consent page; approve. You should return to `/connect` with `Canva connected.`
3. Open **Admin → Upstream MCP servers**, find Canva and press **Refresh tools**. You should see Canva's tools listed as new with defaults: tools that Canva marks read-only on `review`, all others on `popup`. Set one read tool to `auto`, press **Save** and confirm with the passkey.
4. From an AI client call that tool: it should show a card (review) with an **Always allow** button for it; after you press it once, the next call should run without a card. Call a `popup` tool: it should ask first and offer no Always allow.
5. Report back: pass or fail, and if step 2 fails, Canva's error text (the likely cause is that it refuses a redirect URI on this domain; then register an app with Canva, add `oauth: {client_id: ..., client_secret_env: CANVA_CLIENT_SECRET}` to the declaration and write `CANVA_CLIENT_SECRET=<value>` in the QA server's `privacyfence.env`, never in chat, this plan or the declaration).

**2. Connect a real Framer account on the QA server (about 25 minutes).** Same as 1 with `id: framer`, `label: Framer`, `url: https://mcp.framer.com/mcp`; use a Framer account with a test project. If Framer asks for admin approval of MCP connections, say so in the report. Report back: pass or fail with Framer's error text.

**3. Clean up the removed feature on any server that used it (about 10 minutes, per server).** Check the server's log for the warnings of 3.1, delete `org/upstream_claim_keys.json`, `org/upstream_claim_keys.lock` and `org/group_membership.json` from the server's data directory, remove `PRIVACYFENCE_UPSTREAM_MCP_FILE` or `PRIVACYFENCE_UPSTREAM_MCP` (or replace the file with a version 2 one), and remove the `upstream_mcp` section from `org/admin_config.json`. Report back: done, and which of the files existed.

## 6. Risks and open questions

- **The removal is breaking.** Anything built on the claim feature stops working, including the `kh_*` tools and n8n workflows that read or feed the hub through scoped tokens (owner confirmed). Signal: none expected on a server that never declared an upstream; a server that did logs the warnings of 3.1 at start.
- **Vendor redirect-URI policy is unknown** (3.9). If dynamic registration refuses our address the operator declares a `client_id`; if a vendor allows only fixed clients and no declared id exists, the feature cannot serve it. Signal: manual step 1 or 2 fails at Connect.
- **Annotations are untrusted.** A server that marks a write tool `readOnlyHint=true` gets it a `review` default, and `review` runs the call before the person sees anything. Mitigation: the administrator reviews every default before Save, tools new to a list are held, and the list marks a `review` tool that is not read-only; ADR 0329 records the residual.
- **The shared tool list reflects the administrator's account.** A person whose account lacks a listed tool gets the vendor's own `tool_error` (cut to 500 characters). Signal: many `tool_error` rows for one upstream.
- **Live re-registration of policy tables.** The tables are plain module dicts read from several threads (the popup executor and the event loop). `apply` holds a lock for writers and `rebuild_registry` no longer empties the registry, but readers take no lock; a reader can see a half-applied list for one request. The risk is bounded to one call, never a gate lower than `person_gate` of the old or the new list, and a test checks that no reader sees an empty registry.
- **Stored rules after a ceiling is lowered.** A person's rule for a tool later set to `review` or `popup`, or reset by a changed digest, stays in their file as a custom row and matches nothing (the selector checks the ceiling); p13 tests that it survives a load and a later rule write.
- **The administrator's token lists the tools for everyone.** Refresh uses the refreshing administrator's own connection to read a server-wide list; it reads only the tool list, changes nothing at the vendor, and the result is held until a saved review; ADR 0329 says so. A person whose account lacks a tool gets the vendor's error for it.
- **Principal-type enforcement** (`allowed_principal_types`) runs for every call; the old `identity_claim: off` gap disappears with the claim.
- **A scoped token acts as its creator at the vendor** (owner's decision); ADR 0300's tick and per-tool token rules still gate it, and the token form says whose connection it uses.
- **A local upstream** (`http://localhost`) relaxes the metadata rules only for URLs whose own host is loopback (3.3).
- **DNS rebinding** (3.3): the issuer's host is resolved once for the public-address check and again by the connection; ADR 0328 records this.
- **Refresh-token rotation across processes**: the per-path lock is in-process; the org server is one process. If it ever runs more than one, add a file lock.
- **A reverse proxy that logs the callback query** keeps the code (PKCE bounds the damage); the docs say so. A declared client secret in an environment variable is visible to anything that can read the daemon's environment; the docs say to use the orchestrator's secret store.
- **One pull request, not two.** CLAUDE.md's two-PR rule is for a new connector with a live fixture; this is a generic mechanism with no provider-specific code, and the real-account checks are `manual_after`. The `dod_conditional` QA-fixture row (it matches `connectors/**`) does not apply: upstream connectors are not in `CONNECTOR_CHECKS` and have no live fixture; ma1 and ma2 are the human equivalent. `UpstreamOAuthError` is a separate exception type that the client turns into `UpstreamMcpClientError` and the connector into `RuntimeError` subclasses, so the `<Name>ClientError` rule holds.
- **Protected paths:** no phase touches `.tld/toolkit.yaml` or `.github/workflows/**`. `deploy/docker/compose.yaml` is not protected.

## Implementation manifest

```yaml
plan_slug: oauth-upstream-mcp
feature_branch: feature/oauth-upstream-mcp
tracking_issue: 1155
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-connect-canva
    title: Connect a real Canva account on the QA server, refresh and save its tools, and use one with an Always allow rule
    why: CI runs only against the reference OAuth server; only a real Canva shows whether its registration accepts this server's redirect URI and what its tools declare.
  - id: ma2-connect-framer
    title: Connect a real Framer account on the QA server, refresh and save its tools
    why: Framer's registration policy and its admin approval of MCP connections cannot be checked from CI.
  - id: ma3-clean-up-the-removed-feature
    title: Delete the leftover claim key and group files and the old variables on every server that used the removed feature
    why: The server never reads or deletes them, and the key file holds signing seeds.
verify_after_merge:
  - python3 -m pytest tests/unit -q
final_checks:
  - "`tld plan deviations check --file docs/1155/deviations.md --plan docs/1155/plan.md --all` exits 0"
  - ADRs 0327, 0328 and 0329 exist, are Accepted, and are in docs/adr/README.md
  - CHANGELOG.md has entries under `## [Unreleased]` and no new version heading
phases:
  - id: p1-unwire
    title: Unwire the claim feature from the daemon, server, status tool and Admin routes
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/web/server.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/web/routes_admin.py
      - tests/unit/test_daemon_main.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_server_org_mode.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_routes_admin.py
    brief: |
      Design section 3.1 and the "hooks" bullet of section 2 of docs/1155/plan.md. After this phase the claim feature can never be configured or reached, while its modules and their own tests still exist and pass (p3 and p5 delete them). Use grep to find every reference first; the line numbers of section 2 are approximate.
      1. src/privacyfence/daemon_main.py: delete the imports of group_membership, identity_claim, upstream_mcp_config, upstream_mcp_registry, upstream_mcp_runtime and GroupMembershipConnector; delete _configure_upstream_mcp and its call; delete the lines in build_connectors that extend the connector list with upstream_mcp_runtime.connectors_for_principal() and that append GroupMembershipConnector(); delete _upstream_claim_keyset, run_rotate_upstream_claim_key, run_revoke_upstream_claim_key, the two argparse flags --rotate-upstream-claim-key and --revoke-upstream-claim-key and their dispatch.
      2. src/privacyfence/web/server.py: delete the import of routes_upstream_mcp, the claim_signer block that mounts it and appends the upstream lifespan (about lines 728-734), the "upstream_audiences" key of the token JSON (about line 301) and the whole `page["upstreams"]` block of the token page (about lines 331-342; keep the helper _gated_tools if it is a separate function: p17 uses it again).
      3. src/privacyfence/web/mcp_dispatch.py: delete the three lines that add result["upstreams"] to privacyfence_status (about lines 702-705) and the upstream_mcp_runtime import.
      4. src/privacyfence/web/routes_admin.py: delete the approve_group_membership and decline_group_membership actions (their entries in ADMIN_ACTIONS and ADMIN_PARAMS, their handlers, _group_membership_state, _decide_group_membership, _record_group_membership_audit and their state keys), the upstream rows (_upstream_servers_state and its use in the admin state) and the imports of group_membership and upstream_mcp_*.
      5. Update the tests of those four modules: delete only the tests of the deleted behaviour (group-membership approvals, upstream rows in the admin state, privacyfence_status upstreams, claim-key flags, _configure_upstream_mcp, the token page upstream rows); change nothing else and loosen no assertion.
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - grep -n "upstream_mcp\|identity_claim\|group_membership\|routes_upstream_mcp\|upstream-claim-key" src/privacyfence/daemon_main.py src/privacyfence/web/server.py src/privacyfence/web/mcp_dispatch.py src/privacyfence/web/routes_admin.py prints nothing
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p2-ui-membership
    title: Remove the membership page, the audiences UI and the group-membership connector
    depends_on: [p1-unwire]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/connectors/group_membership.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/scoped_token_proposals.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - tests/unit/test_settings_window_html.py
      - tests/unit/connectors/test_group_membership_connector.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_auto_accept.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/unit/test_runtime_credentials.py
      - tests/unit/test_website_connector_pages.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/integration/test_settings_scoped_token_form_browser.py
    brief: |
      Design section 3.1 and the "hooks" bullet of section 2 of docs/1155/plan.md. Keep everything of ADR 0300 (the gated-tool tick: upstream_approvals, _normalise_upstream_approvals, _checked_upstream_approvals, the upstream.approved_tool rule, the `stGated` form state and the "Upstream tools that change data or release results unseen" fieldset). The scoped-token audience fields stay until p4.
      1. src/privacyfence/settings_window_html.py: delete the group membership page (renderMembership, membershipPendingCardHtml, its tab entry, its approve_group_membership and decline_group_membership posts), the read-only Admin upstream cards (renderUpstreams, upstreamCardHtml, upstreamAudiencesCardHtml, UPSTREAM_AUDIENCES_ERROR_TEXT, UPSTREAM_CALLER_TEXT, upstreamToolRow) but leave a stub `renderUpstreams(state)` returning `<div class="pf-page"><h1 class="pf-page-title">Upstream MCP servers</h1></div>` (p18 fills it) and keep the 'upstreams' Admin tab entry, and the token form's audiences parts (stAudiences state, scopedAudiencesHtml, scopedChosenAudiences, the upstream_audiences post field, the audiences click handler; the form posts no upstream_audiences any more and the server side defaults it to empty until p4).
      2. git rm src/privacyfence/connectors/group_membership.py and tests/unit/connectors/test_group_membership_connector.py; src/privacyfence/auto_accept.py: delete the privacyfence_update_group_membership entry (about line 415); src/privacyfence/scoped_tokens.py and scoped_token_proposals.py: delete the GROUP_MEMBERSHIP_TOOL import and every use of it (the refusal in the proposal path included) and nothing else.
      3. scripts/generate_tools_reference.py (about lines 60, 81, 234-236) and docs/tools-reference.md (about lines 33-35 and 369): remove the privacyfence_update_group_membership tool; regenerate the reference with the script so tests/unit/test_docs_tools_reference.py passes; fix tests/unit/connectors/test_readme_manifest_alignment.py and tests/unit/test_website_connector_pages.py (remove "group_membership" from NO_PAGE_MODULES) if they name the removed connector.
      4. tests/unit/web/test_routes_org_settings.py (about lines 1334-1487): delete only the tests that use upstream_audiences, privacyfence_update_group_membership, upstream_mcp_registry.register or held_audiences.
      5. Add `test_a_token_naming_an_unregistered_tool_loads_and_keeps_the_name` to tests/unit/test_scoped_tokens.py (a stored token whose tools include a name absent from TOOL_TO_GATE loads, keeps that name in `tools`, and a re-save writes it back); if the loader drops or rejects such a name, change it to keep it, and make the tools/list filter in web/routes_mcp.py (it intersects with the specs) the only place the name disappears.
      6. tests/unit/test_runtime_credentials.py (about lines 154-162): delete `test_a_delivered_sync_token_is_refused` (it tests the removed tool's refusal text).
      7. Update the other tests of these modules: delete only the tests of deleted behaviour, change nothing else and loosen no assertion.
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - ls src/privacyfence/connectors/group_membership.py fails with the file reported missing
      - grep -n "approve_group_membership\|decline_group_membership\|stAudiences\|scopedAudiencesHtml\|scopedChosenAudiences" src/privacyfence/settings_window_html.py prints nothing
      - grep -rn "GROUP_MEMBERSHIP_TOOL\|privacyfence_update_group_membership" src scripts docs/tools-reference.md --exclude=group_membership.py prints nothing
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p3-delete-core
    title: Delete the claim-based upstream core modules and their tests
    depends_on: [p2-ui-membership]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_config.py
      - src/privacyfence/upstream_mcp_registry.py
      - src/privacyfence/upstream_mcp_runtime.py
      - src/privacyfence/upstream_mcp_client.py
      - src/privacyfence/connectors/upstream_mcp.py
      - src/privacyfence/org_mode.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/scoped_token_proposals.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_server.py
      - tests/integration/test_org_docker_deploy_files.py
      - tests/conftest.py
      - tests/unit/test_upstream_mcp_config.py
      - tests/unit/test_upstream_mcp_registry.py
      - tests/unit/test_upstream_mcp_runtime.py
      - tests/unit/test_upstream_mcp_client.py
      - tests/unit/test_upstream_mcp_examples.py
      - tests/unit/test_upstream_mcp_reference_server.py
      - tests/unit/connectors/test_upstream_mcp_connector.py
      - tests/integration/test_upstream_mcp_org.py
      - tests/unit/test_gate.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_systemic_gate_invariants.py
      - tests/unit/web/test_tool_schema_portability.py
      - tests/unit/test_org_mode.py
    brief: |
      Design section 3.1 of docs/1155/plan.md. The five modules are dead after p1; delete them (git rm) and fix every remaining reference. The new feature re-creates modules of the same names in p9-p15, copying from `git show f6c95e6b0:<path>` where a brief says so.
      1. Delete the five source files and the eight test files listed (git rm). Delete `tests/unit/test_upstream_mcp_examples.py` and `tests/unit/test_upstream_mcp_reference_server.py` too (they test the examples and reference server removed in p7; they import contrib/verifier which p5 deletes).
      2. src/privacyfence/org_mode.py: in connector_enabled_by_org delete the `upstream_mcp_runtime.upstream_ids()` branch and its function-local import.
      3. src/privacyfence/web/routes_settings.py: delete the held_audiences calls and every other use of upstream_mcp_runtime in the create-token paths (about lines 900-956 and 992-996); a person's request still passes `upstream_audiences` as an empty mapping until p4 removes the field. Then grep src, tests and scripts for any remaining import of the five modules (known: org_mode.py, routes_settings.py, test files) and fix each by deleting only what used the removed behaviour.
      4. src/privacyfence/scoped_token_proposals.py (about lines 157-164): replace the `upstream_mcp_runtime.upstream_ids()` test of whether a proposal names an upstream tool by `auto_accept.TOOL_TO_OPERATION.get(tool) in policy.scopes.UPSTREAM_DECLARATIONS` (that table is filled by the new registry in p13; until then it is empty), keeping the refusal text and behaviour; tests/unit/test_scoped_token_proposals.py (about line 350): rewrite the test of that refusal to fill UPSTREAM_DECLARATIONS and TOOL_TO_OPERATION directly with monkeypatch. Fix the module-level imports of the removed modules in tests/unit/connectors/test_readme_manifest_alignment.py (about line 40: drop the upstream connector from the manifest list), tests/unit/web/test_mcp_dispatch.py (lines about 24 and 35: delete `TestScopedUpstreamApproval`, p13 re-covers it), tests/unit/web/test_server.py (about lines 886-990: delete `TestScopedTokenPageData`'s upstream approval tests, p17 re-covers them) and tests/integration/test_org_docker_deploy_files.py (about line 65: only the import of the removed module; p7 does the rest).
      5. tests/conftest.py: delete the autouse fixture _restore_upstream_tool_tables (lines about 213-220) and its imports; p13 adds a replacement.
      6. tests/unit/test_gate.py (about line 5164), tests/unit/test_scoped_tokens.py (about line 1535), tests/unit/test_systemic_gate_invariants.py and tests/unit/web/test_tool_schema_portability.py, tests/unit/test_org_mode.py: delete only the tests or parametrizations that registered or exercised upstream tools through the deleted modules; p13, p14 and p15 add tests that cover the gate, scoped-token approval and schema pass-through behaviours again with the new modules. Loosen no other assertion.
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - ls src/privacyfence/upstream_mcp_config.py src/privacyfence/upstream_mcp_registry.py src/privacyfence/upstream_mcp_runtime.py src/privacyfence/upstream_mcp_client.py src/privacyfence/connectors/upstream_mcp.py fails with all five reported missing
      - grep -rnE "(from|import) .*(upstream_mcp_(config|registry|runtime|client)|connectors\.upstream_mcp)" src tests scripts prints nothing
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p4-token-fields
    title: Remove the audience fields of scoped tokens and their runtime credentials
    depends_on: [p3-delete-core]
    complexity: S
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/scoped_token_proposals.py
      - src/privacyfence/runtime_credentials.py
      - src/privacyfence/web/routes_settings.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/unit/test_runtime_credentials.py
    brief: |
      Design section 3.1 and the "hooks" bullet of section 2 of docs/1155/plan.md. Keep every part of ADR 0300 (upstream_approvals, _checked_upstream_approvals, the upstream.approved_tool rule).
      1. src/privacyfence/scoped_tokens.py: delete the upstream_audiences field of ScopedToken and of the create path, _normalise_upstream_audiences and its JSON conversion, chosen_audiences, and the claim-only text of the comment at lines 81-89 (check what `principal_email` and any email-verified field are still used for before deleting anything; keep what other code reads). The loader must ignore an `upstream_audiences` key in a stored token (test: `test_a_stored_token_with_upstream_audiences_still_loads`).
      2. src/privacyfence/scoped_token_proposals.py: delete the upstream_audiences comparison (about lines 198-202); keep the refusal of upstream tools and of upstream approvals in a proposal. src/privacyfence/runtime_credentials.py: delete the upstream_audiences argument (about line 333). src/privacyfence/web/routes_settings.py: delete the upstream_audiences parameter handling of the create-token paths.
      3. Update the tests of those modules: delete only the tests of deleted behaviour, change nothing else.
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - grep -n "upstream_audiences\|chosen_audiences" src/privacyfence/scoped_tokens.py src/privacyfence/scoped_token_proposals.py src/privacyfence/runtime_credentials.py src/privacyfence/web/routes_settings.py prints only the loader's tolerant read in scoped_tokens.py
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p5-delete-claim-modules
    title: Delete the identity claim, claim keys, audiences, group membership and the verifier
    depends_on: [p4-token-fields]
    complexity: M
    touches:
      - src/privacyfence/identity_claim.py
      - src/privacyfence/upstream_claim_keys.py
      - src/privacyfence/upstream_audiences.py
      - src/privacyfence/group_membership.py
      - src/privacyfence/web/routes_upstream_mcp.py
      - contrib/verifier
      - docs/identity-claim.md
      - tests/conftest.py
      - tests/unit/test_identity_claim.py
      - tests/unit/test_upstream_claim_keys.py
      - tests/unit/test_upstream_audiences.py
      - tests/unit/test_group_membership.py
      - tests/unit/test_pf_claim_verify.py
      - tests/unit/web/test_routes_upstream_mcp.py
    brief: |
      Design section 3.1 of docs/1155/plan.md. These modules are dead after p3 and p4.
      1. git rm the five source files, the directory contrib/verifier (including req_test_vectors.json and any __pycache__) and the six test files listed that exist (check each with ls; a file that does not exist is skipped).
      2. docs/identity-claim.md (about lines 169 and 239): turn the two links to `../contrib/verifier/req_test_vectors.json` and `../contrib/verifier/pf_claim_verify.py` into plain code spans so tests/unit/test_docs_links.py keeps passing; p8 replaces the whole document.
      3. tests/conftest.py: delete the group_membership import (about line 37) and the `set_store(None)` reset (about line 149).
      4. src/privacyfence/audit_log.py is NOT touched: the claim_*, upstream_status and metadata fields stay as legacy (3.1).
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - grep -rnE "(from|import) .*(identity_claim|upstream_claim_keys|upstream_audiences|group_membership|pf_claim_verify)" src scripts tests prints nothing
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p6-claim-helpers
    title: Remove the claim-only client-id helper
    depends_on: [p5-delete-claim-modules]
    complexity: S
    touches:
      - src/privacyfence/agent_identity.py
      - src/privacyfence/web/routes_mcp.py
      - tests/unit/test_agent_identity.py
      - tests/unit/web/test_routes_mcp_agent_identity.py
    brief: |
      Design section 3.1 of docs/1155/plan.md.
      1. src/privacyfence/agent_identity.py: delete current_client_id, client_id_scope and the contextvar behind them (about lines 288-309) and the docstring mention at line 22. src/privacyfence/web/routes_mcp.py: delete the client_id_scope import and the `with client_id_scope(...)` use (about lines 53 and 474), keeping the code inside the block.
      2. Update tests/unit/test_agent_identity.py and tests/unit/web/test_routes_mcp_agent_identity.py by deleting only the tests of the removed helpers.
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - grep -rn "client_id_scope\|current_client_id" src tests prints nothing
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p7-delete-assets
    title: Delete the old reference server, examples and deployment lines, and the admin-config default
    depends_on: [p6-claim-helpers]
    complexity: M
    touches:
      - scripts/upstream_mcp_reference_server.py
      - contrib/examples/knowledge-hub
      - deploy/docker/compose.yaml
      - deploy/docker/privacyfence.env.example
      - src/privacyfence/admin_config.py
      - pyproject.toml
      - tests/integration/test_org_docker_deploy_files.py
      - tests/unit/test_admin_config.py
    brief: |
      Design section 3.1 of docs/1155/plan.md.
      1. git rm scripts/upstream_mcp_reference_server.py and the directory contrib/examples/knowledge-hub.
      2. deploy/docker/compose.yaml (about lines 22 and 42) and deploy/docker/privacyfence.env.example (about 79-80): delete the commented upstream lines; tests/integration/test_org_docker_deploy_files.py (about line 77): delete the special case for them.
      3. src/privacyfence/admin_config.py: delete the `"upstream_mcp": {}` default (about line 157) and anything that reads it; tests/unit/test_admin_config.py: delete only the tests of that section and add `test_a_leftover_upstream_mcp_section_is_kept_on_load_and_save` (an admin_config.json holding an upstream_mcp object loads, and apply_change of another action re-saves it untouched).
      4. pyproject.toml: keep the httpx2 dependency (the new client uses it) and change its comment (line 18) to say it is for the OAuth upstream MCP client.
    acceptance:
      - python3 -m pytest tests/unit tests/integration/test_org_docker_deploy_files.py -q passes
      - ls scripts/upstream_mcp_reference_server.py contrib/examples/knowledge-hub fails with both reported missing
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p8-docs-cleanup
    title: Clean the docs and changelog of the removed feature and leave two tombstones
    depends_on: [p7-delete-assets]
    complexity: M
    touches:
      - docs/upstream-mcp.md
      - docs/identity-claim.md
      - docs/how-it-works.md
      - docs/org-mode-setup-guide.md
      - docs/org-mode-docker.md
      - docs/configuration-reference.md
      - docs/testing-policy.md
      - docs/README.md
      - CHANGELOG.md
      - tests/unit/test_docs_configuration_reference.py
      - tests/unit/test_docs_references_exist.py
    brief: |
      Design section 3.1 of docs/1155/plan.md. Two docs stay as tombstones because ADRs, older plans and the docs link tests refer to them; p23 rewrites docs/upstream-mcp.md.
      1. Replace the whole content of docs/identity-claim.md with `# The upstream identity claim (removed)` and one paragraph (three lines at most): the claim-based upstream feature was removed in 8.x, the document is kept only so links to it keep working, and ADR 0327 says why (name the ADR in plain text, with no link: it is written in p21). Replace docs/upstream-mcp.md with `# Upstream MCP servers behind PrivacyFence` and the same kind of paragraph saying the claim-based feature was removed and this page is rewritten for OAuth upstream servers. If tests/unit/test_docs_links.py or test_docs_references_exist.py reports a missing anchor of either file in an ADR or plan, add an empty `## <that exact heading>` section with the one line `Removed.` to the tombstone.
      2. Remove every remaining mention of the removed feature from docs/how-it-works.md (about lines 115-116), docs/org-mode-setup-guide.md (about 981), docs/org-mode-docker.md (about 308, 328, 423-428, 468-469), docs/configuration-reference.md (about 314-315: delete the two PRIVACYFENCE_UPSTREAM_MCP rows, p23 adds the new ones), docs/testing-policy.md (about 207-209) and docs/README.md (about 37-41; keep the row of docs/upstream-mcp.md, now describing the tombstone as `removed; rewritten for OAuth upstream servers`, and delete the row of docs/identity-claim.md only if the index rules allow a tombstone row to be absent: otherwise keep it); keep each edited sentence true without the feature.
      3. CHANGELOG.md: under `## [Unreleased]` delete the entries that announce the claim-based upstream feature (about lines 48-54 and 59-60: the audiences from the upstream, the Admin page for upstream servers, upstream MCP servers behind PrivacyFence, declare from a YAML file, audiences from Workspace groups, group membership and the identity-claim entries; read each line before deleting and keep any line that is about something else); no new entry here (p23 adds one); never a version heading.
      4. tests/unit/test_docs_links.py (the link and anchor check), test_docs_references_exist.py and test_docs_configuration_reference.py must pass: fix a link or a documented variable they report.
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - grep -rln "PRIVACYFENCE_UPSTREAM_MCP" docs/*.md deploy scripts contrib src prints nothing
      - grep -c "" docs/upstream-mcp.md prints a number below 15
      - ruff check . passes
  - id: p9-config
    title: The version 2 operator file
    depends_on: [p8-docs-cleanup]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_config.py
      - tests/unit/test_upstream_mcp_config.py
    brief: |
      Design section 3.2 of docs/1155/plan.md exactly (keys, limits, problem texts and the version messages are fixed there). Create src/privacyfence/upstream_mcp_config.py, copying the loader plumbing named in 3.2 from `git show f6c95e6b0:src/privacyfence/upstream_mcp_config.py` (_parse_text, _read_file, load_declared, parse_inline, _UniqueKeyLoader, _json_pairs, _has_control, _MAX_BYTES, ENV_VAR, ENV_VAR_INLINE, BUILTIN_NAMES and the id-collision rule) and nothing else of it.
      1. Module docstring (ADR 0327), ENV_VAR = "PRIVACYFENCE_UPSTREAM_MCP_FILE", ENV_VAR_INLINE = "PRIVACYFENCE_UPSTREAM_MCP", frozen dataclasses UpstreamOAuthSettings and UpstreamServer of 3.2, load_upstreams(path), parse_inline(text), load_declared() (None when neither variable is set; both set raises `Set <ENV_VAR> or <ENV_VAR_INLINE>, not both.`), parse_upstreams(data, *, source), and `leftovers() -> list[str]` returning the three warning texts of 3.1 for each of org_dir()/"upstream_claim_keys.json", org_dir()/"group_membership.json" that exists and, given the admin config dict, an upstream_mcp section (signature `leftovers(admin_config: dict | None = None)`). Every problem raises org_mode.ConfigurationError. The built-in name set is the one of `git show f6c95e6b0` plus "docs" and "sheets" (3.2); an id equal to either raises `upstream id "<id>" collides with the built-in connector "drive"`.
      2. Tests in tests/unit/test_upstream_mcp_config.py: a valid file and inline text (YAML and JSON); each row of the 3.2 table accepted and refused with its exact problem text (parametrized); version 1 and version 3 messages; unknown keys (tools, audience, identity_claim, top-level claim and membership); a repeated key; a file over 256 KiB; both variables set; a duplicate id and an id that collides with a built-in connector (`gmail`, `slack`, `docs`, `sheets`); http to a non-loopback host refused and to localhost accepted; more than 8 upstreams; defaults (allowed_principal_types both, empty oauth); leftovers() with each file present and absent.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_config.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_config.py -q --cov=privacyfence.upstream_mcp_config --cov-branch --cov-fail-under=100 passes
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "this release reads version 2 only" src/privacyfence/upstream_mcp_config.py matches
  - id: p10-oauth-tokens
    title: The token module - errors, capped HTTP helper, token files, authorize URL, code exchange, token provider
    depends_on: [p9-config]
    complexity: M
    touches:
      - src/privacyfence/upstream_oauth_tokens.py
      - tests/unit/test_upstream_oauth_tokens.py
      - tests/unit/test_systemic_gate_invariants.py
    brief: |
      Design section 3.5 of docs/1155/plan.md exactly. Read src/privacyfence/miro_auth.py (save_token_file, MiroTokenProvider, _token_request) as a pattern, and mcp/shared/auth.py (OAuthToken) and mcp/shared/auth_utils.py (calculate_token_expiry) in the installed SDK first.
      1. Create src/privacyfence/upstream_oauth_tokens.py with a module docstring (ADR 0327, 0328) and: UpstreamOAuthError, OAuthClient, TokenRecord, http_json, origin_of(url) (scheme://host[:port], lower case), token_path, token_paths, load_token, save_token, delete_token, build_authorize_url, async exchange_code, UpstreamTokenProvider; signatures, fields and behaviours exactly as in 3.5. No module-level mutable state.
      2. Add ("upstream_oauth_tokens", None, "save_token") to TOKEN_WRITE_SITES in tests/unit/test_systemic_gate_invariants.py and make save_token write through secure_files.atomic_write_json so that invariant passes; read the test first and follow what it checks for the other entries.
      3. Tests in tests/unit/test_upstream_oauth_tokens.py with httpx2.MockTransport: http_json returns status, parsed object and headers, returns None for a non-object body, refuses a body over 64 KiB, does not follow a redirect, and maps a transport error to unreachable; token file round trip, mode 0600 and 0700 directory; load_token on an absent, malformed and wrong-version file returns None; token_paths orders newest first and ignores other upstream ids; origin_of for https, an explicit port and http loopback; build_authorize_url contains each parameter and omits scope and resource when None; exchange_code success for each of the three auth methods (assert the Authorization header or form fields), expires_in absent gives expires_at None, a token answer without access_token is unreachable, 400 gives auth_expired, 500 gives unreachable, and no error text contains the code, verifier or secret (sentinel strings); provider: a fresh token is returned without a request, a token within 60 seconds is refreshed and saved, a rotated refresh token replaces the old and an absent one keeps it, two concurrent bearer calls refresh once, force_refresh refreshes, a 400 deletes the file and raises auth_expired, a 400 whose JSON error is invalid_client also calls on_invalid_client with the upstream id, a 500 keeps the file and raises unreachable, a client with another client_id raises auth_expired and deletes the file, a stale token without refresh token raises auth_expired, an absent file raises not_connected, expires_at None never refreshes; test_the_sdk_names_this_module_uses_exist imports OAuthToken from mcp.shared.auth and calculate_token_expiry from mcp.shared.auth_utils.
      4. Stop condition: if calculate_token_expiry or OAuthToken differ from what 3.5 assumes (read them), stop with status=blocked and quote the signatures.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_oauth_tokens.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit/test_upstream_oauth_tokens.py -q --cov=privacyfence.upstream_oauth_tokens --cov-branch --cov-fail-under=100 passes
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "class UpstreamTokenProvider" src/privacyfence/upstream_oauth_tokens.py matches
  - id: p11-oauth-discovery
    title: The discovery and client-registration module
    depends_on: [p10-oauth-tokens]
    complexity: M
    touches:
      - src/privacyfence/upstream_oauth_discovery.py
      - tests/unit/test_upstream_oauth_discovery.py
    brief: |
      Design sections 3.3 and 3.4 of docs/1155/plan.md exactly (the URL rule, the DNS rule, the order of steps, the checks and the refusal kinds are fixed there). Read src/privacyfence/grist_auth.py (discover, _endpoint, _discovery_document) as a pattern, src/privacyfence/admin_config.py _require_public_https_host (the DNS check pattern; do not import it, the rule here is _require_public of 3.3), and these installed-SDK helpers before using them: mcp.client.auth.utils build_protected_resource_metadata_discovery_urls, build_oauth_authorization_server_metadata_discovery_urls, extract_resource_metadata_from_www_auth, validate_metadata_issuer; mcp.client.auth.exceptions OAuthFlowError; mcp.shared.auth ProtectedResourceMetadata, OAuthMetadata, OAuthClientInformationFull; mcp.shared.auth_utils check_resource_allowed, resource_url_from_server_url.
      1. Create src/privacyfence/upstream_oauth_discovery.py with a module docstring (ADR 0328), the frozen dataclass OAuthEndpoints, _checked_url, _resolve, _require_public, async discover(upstream, *, transport=None), and class RegistrationStore with ensure_client, client_for and forget (3.4). Import UpstreamOAuthError, OAuthClient and http_json from upstream_oauth_tokens. No module-level mutable state; the per-upstream asyncio.Lock dict is an instance attribute. Never use the SDK fallbacks named in 3.3 step 9; never use the helper create_client_registration_request (build the registration request here, 3.4).
      2. The registration cache directory is the constructor argument; files are written by a method named _write with secure_files.atomic_write_json after secure_files.secure_mkdir, mode 0600. This is a client registration, not a user token, so it is not added to TOKEN_WRITE_SITES.
      3. Tests in tests/unit/test_upstream_oauth_discovery.py with httpx2.MockTransport servers and a monkeypatched _resolve (default: a public address): the happy path for a Canva-shaped server (issuer = resource origin) and a Framer-shaped one (issuer with a path on another host than the MCP URL, metadata at the path-inserted well-known URL); a 401 hint on the upstream's host is followed and a hint on another host is skipped; no hint falls back to the well-known URLs; refusals for a mismatching resource and a document without resource or authorization_servers; a pinned issuer not listed refused, listed used (also when one side has a trailing slash and the other has not); an issuer mismatch in the metadata refused (OAuthFlowError caught); each endpoint on another host refused, on another port refused, http refused unless the upstream URL is loopback http and the endpoint host is itself loopback (a loopback upstream whose metadata names http://169.254.169.254 or an https private address is refused), an IP literal in each private range (10.0.0.1, 127.0.0.1, 169.254.169.254, 192.168.1.1, ::1, fc00::1) refused unless loopback, userinfo and fragment refused, a single-label host and a .internal host refused; _require_public refuses an issuer whose _resolve answer holds a private or loopback address, maps a resolution error to unreachable, and is skipped when loopback; S256 missing or the list absent refused; scope selection (declared, scopes_supported, none); token_auth_methods default and iss_supported; redirects are not followed (a 302 answer is a refusal); a body over 64 KiB refused; a transport error is unreachable; the SDK fallbacks are never requested (assert the recorded request URLs). RegistrationStore: declared client_id with and without a secret (environment variable via monkeypatch), declared name with an empty variable not_configured, a first registration writes the file with mode 0600 and sends the exact JSON body of 3.4 for each of the three method choices, a second call with the same issuer and redirect URI sends no request, a changed redirect URI or issuer registers again, an expiring secret registers again, concurrent ensure_client calls register once, a registration answer without client_id or with other redirect_uris is registration_failed, no registration_endpoint without a declared id is registration_failed, client_for returns declared, cached, and raises not_configured otherwise, forget removes the file. test_the_sdk_names_this_module_uses_exist imports every SDK name listed above.
      4. Stop condition: if a helper's signature differs from `build_protected_resource_metadata_discovery_urls(www_auth_url, server_url)`, `build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, server_url)`, `extract_resource_metadata_from_www_auth(response)`, `validate_metadata_issuer(oauth_metadata, expected_issuer)`, stop with status=blocked and quote the signature.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_oauth_discovery.py -q passes
      - python3 -m pytest tests/unit/test_upstream_oauth_discovery.py -q --cov=privacyfence.upstream_oauth_discovery --cov-branch --cov-fail-under=100 passes
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "def _checked_url" src/privacyfence/upstream_oauth_discovery.py matches
  - id: p12-tool-store
    title: The tool list model - classification, defaults, digests, diff and the saved store
    depends_on: [p8-docs-cleanup]
    complexity: M
    touches:
      - src/privacyfence/upstream_tools.py
      - tests/unit/test_upstream_tools.py
    brief: |
      Design section 3.6 of docs/1155/plan.md exactly. The sentences are fixed in 3.6; `_skip_reason` in `git show f6c95e6b0:src/privacyfence/upstream_mcp_client.py` is the pattern for the schema checks.
      1. Create src/privacyfence/upstream_tools.py (module docstring: ADR 0329; no logging, no network, no module-level mutable state) with the frozen dataclasses ToolEntry, SavedTool, SavedList(upstream_id, url, saved_at, tools), ToolDiff, and: classify, person_gate, unsupported_reason, tool_digest, declaration_digest, diff, class ToolStore(directory) with load and save, and the constants GATES = ("review", "popup", "auto"), MAX_TOOLS = 50. `ToolStore.save(upstream_id, url, tools, now)` writes the url; `diff` takes the saved tuple and the fetched tuple of (ToolEntry, unsupported_reason or None) pairs.
      2. Tests in tests/unit/test_upstream_tools.py: classify for every combination of read_only in (True, False, None) and destructive in (True, False, None) (readOnly True with destructive True is popup and delete; readOnly True alone is review and read; destructive False alone is popup and update; no hints is popup and delete); person_gate for each configured gate and class default (auto gives the class default); unsupported_reason returns the exact sentence of 3.6 for a bad name, a 49-character name, an exposed name over 64 characters, a non-object schema, a schema with a reason property, with $ref, with $defs, with definitions nested two levels deep, over 32 KiB, and None for a good tool; tool_digest changes with each field and is stable across key order; declaration_digest changes with each of its five inputs and equals a hard-coded 32-character literal for a fixed sample (compute the literal when writing the test); diff classifies new, changed, removed, unchanged and unsupported, and puts tools beyond the 50th into unsupported with the reason `more than 50 tools`; ToolStore round trip (including the url) with mode 0600, absent, malformed and wrong-version files return None, an atomic write replaces the file.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_tools.py -q passes
      - python3 -m pytest tests/unit/test_upstream_tools.py -q --cov=privacyfence.upstream_tools --cov-branch --cov-fail-under=100 passes
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "def person_gate" src/privacyfence/upstream_tools.py matches
  - id: p13-policy-hooks
    title: Live registration of upstream tools into the policy tables, the per-tool Always allow scope and the token rule route
    depends_on: [p9-config, p12-tool-store]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_registry.py
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/policy/describe.py
      - src/privacyfence/policy/scope_rows.py
      - src/privacyfence/policy/outdated.py
      - src/privacyfence/scoped_tokens.py
      - tests/conftest.py
      - tests/unit/test_upstream_mcp_registry.py
      - tests/unit/test_upstream_policy_scopes.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/unit/web/test_mcp_dispatch.py
    brief: |
      Design section 3.7 of docs/1155/plan.md exactly. Read first: src/privacyfence/policy/propose.py (ProposableScope, _scope, PROPOSABLE_SCOPES, _by_group, operations_for, _candidate_value, scope_needs_value, proposals_for, rules_for_proposal, rules_for_scope_group), policy/scopes.py (NEW_SCOPE_SELECTORS, UPSTREAM_DECLARATIONS, TOKEN_ONLY_PREDICATES), policy/registry.py (rebuild_registry, TOOL_REGISTRY), policy/describe.py (connector_label), policy/scope_rows.py (scope_of_rule, row_from_dict, add_rules), policy/outdated.py (missing_operations), scoped_tokens.py (_checked_upstream_approvals, _prepare, the token rows), and `git show f6c95e6b0:src/privacyfence/upstream_mcp_registry.py` (the module being replaced).
      1. policy/registry.py: change rebuild_registry to build the new mapping first, `TOOL_REGISTRY.update(new)`, then delete the keys not in `new` (no moment with an empty registry); keep it the same object.
      2. policy/scopes.py: add UPSTREAM_AUTO_TOOLS (set[str]), _upstream_always_allow_matches(value, ctx) and the NEW_SCOPE_SELECTORS entry "upstream.always_allow" of 3.7.
      3. policy/propose.py: make _candidate_value look in `scopes.SCOPE_SELECTORS` and then `scopes.NEW_SCOPE_SELECTORS`; add the field `fixed_value: Any = NO_VALUE` to ProposableScope, make scope_needs_value false for a scope with a fixed value and rules_for_scope_group use it instead of the given value; add set_upstream_scopes(scopes) of 3.7 (the static tuple is kept in a new module constant _STATIC_SCOPES taken from the present PROPOSABLE_SCOPES value); make every in-package reader use `propose.PROPOSABLE_SCOPES` through the module attribute (grep for `from .propose import PROPOSABLE_SCOPES` and fix). policy/describe.py: add set_connector_labels(mapping) and make connector_label consult it for an unknown connector id; if another describe function raises for the predicate upstream.always_allow (the confirmation text of the card's Always allow button), make it name the rule as `Always allow <Label>: <tool>` without an error.
      4. policy/scope_rows.py: scope_of_rule returns None for a rule whose predicate is upstream.always_allow (so it is stored as a custom row). policy/outdated.py: missing_operations returns an empty frozenset for that predicate.
      5. scoped_tokens.py: change _checked_upstream_approvals as the "Scoped tokens" paragraph of 3.7 says (its signature gains the token's rows; the refusal text is the one given there); only an administrator may still send an approval; the token's own rows are the compiled rows of the request, so in `create` call `_checked_upstream_approvals` after `_prepare` (today it is called before, at about lines 759-762). A row that only a copy or duplicate request resolves later (`_check_locked`) does not count as coverage. `scoped_token_proposals.py` keeps refusing an AI proposal that names any upstream tool, an auto tool with a rule included (the check is the registry-based one p3 left; do not weaken it).
      6. Create src/privacyfence/upstream_mcp_registry.py: class `UpstreamRegistry` (an RLock and, per upstream, the names, operations and scopes it added) with `apply(upstream, saved)`, `clear(upstream_id)` and `unregister_all()` exactly as 3.7 says (the EFFECT_BY_TOOL template, _TOOL_LAYOUT, UPSTREAM_DECLARATIONS, UPSTREAM_AUTO_TOOLS, set_upstream_scopes with one ProposableScope per auto tool whose `excludes` is every other operation of that connector, set_connector_labels, rebuild_registry, refresh_all_operations); plus module functions `get_registry() -> UpstreamRegistry` (one process-wide instance created lazily) and `reset()` (unregister_all and drop the instance). A name in TOOL_TO_GATE that this registry did not add raises org_mode.ConfigurationError(`Tool "<name>" collides with an existing tool`).
      7. tests/conftest.py: add an autouse fixture that calls upstream_mcp_registry.reset() after each test.
      8. Tests. tests/unit/test_upstream_mcp_registry.py: apply registers every table for a review, a popup and an auto tool and clear restores each table to its prior content (snapshot test); a second apply replaces the first; a collision raises; popup tools get the effect template and review tools do not; TOOL_TO_GATE never holds auto; UPSTREAM_AUTO_TOOLS holds only auto tools; the registry is never empty during apply (a thread reads TOOL_REGISTRY in a loop while apply runs repeatedly; assert it never saw zero entries). tests/unit/test_upstream_policy_scopes.py: for an auto tool proposals_for offers exactly one proposal and rules_for_proposal gives a rule with predicate upstream.always_allow, value the exposed name and operations equal to that tool's operation; for a review or popup tool proposals_for is empty; two auto tools of one upstream give two rules with different ids and add_rules keeps them as two custom rows (a second Always allow does not extend the first); the selector matches only for a tool in UPSTREAM_AUTO_TOOLS and for ctx.tool equal to the value or empty; after the tool is lowered the stored rules match nothing, still load, and survive the person's next rule write; outdated.missing_operations is empty for the rule; scope_catalogue() lists `<Label> — <Tool>` for an auto tool; gate-level: with a matching rule a review call returns without a card (use the fakes test_gate.py already uses) and a scoped token whose rows hold the per-tool rule passes check_scoped_upstream_approval (the preflight builds ctx.tool="") while a token with neither rule nor approval does not; a scoped token with an upstream.approved_tool value built from UPSTREAM_DECLARATIONS passes while one with an older digest does not. tests/unit/test_scoped_token_proposals.py: a proposal naming an upstream tool (an auto tool carrying its per-tool rule included) is refused with UPSTREAM_REFUSED_MESSAGE. tests/unit/web/test_mcp_dispatch.py: a `TestScopedUpstreamApproval` class that re-covers p3's deletion: the dispatcher calls check_scoped_upstream_approval before connector.call, with a fake connector and the new registry. tests/unit/test_scoped_tokens.py: creating a token with an upstream tool is refused for a non-administrator without a rule (new text), accepted with the per-tool rule, and accepted for an administrator with a tick; a rule for a tool not in UPSTREAM_AUTO_TOOLS does not cover it.
      9. Stop condition: if add_rules, the Settings loader or the rule writer drops or rewrites a custom row of predicate upstream.always_allow, or describe/propose raises for it in a way step 3 does not cover, stop with status=blocked and quote the code.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_registry.py tests/unit/test_upstream_policy_scopes.py tests/unit/test_scoped_tokens.py -q passes
      - python3 -m pytest tests/unit -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_registry.py -q --cov=privacyfence.upstream_mcp_registry --cov-branch --cov-fail-under=100 passes
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "upstream.always_allow" src/privacyfence/policy/scopes.py src/privacyfence/policy/propose.py src/privacyfence/policy/scope_rows.py src/privacyfence/policy/outdated.py matches in all four files
  - id: p14-client-connector
    title: The OAuth upstream client, BearerAuth and the connector
    depends_on: [p10-oauth-tokens, p12-tool-store, p13-policy-hooks]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_client.py
      - src/privacyfence/connectors/upstream_mcp.py
      - tests/unit/test_upstream_mcp_client.py
      - tests/unit/connectors/test_upstream_mcp_connector.py
    brief: |
      Design section 3.8 of docs/1155/plan.md exactly. Read `git show f6c95e6b0:src/privacyfence/upstream_mcp_client.py` and `git show f6c95e6b0:src/privacyfence/connectors/upstream_mcp.py` first: the call plumbing, result limits, schema pass-through, error classes and audit entry are copied from them as 3.8 lists; everything about the claim, signer, caller, audiences, identity_claim and the `auto` gate is dropped.
      1. Create src/privacyfence/upstream_mcp_client.py: CallerNotAllowedError(Exception) with the attribute principal_type (the runtime raises it, the connector catches it), UpstreamMcpClientError(kind, detail=""), CallResult(text, structured, dropped_blocks), BearerSource, BearerAuth, UpstreamMcpClient and UpstreamToolCatalog(upstream, saved) whose specs() builds one ToolSpec per saved tool (name `<id>_<tool>`, the pass-through schema plus the required `reason` property, description cut to 700 characters with control characters removed, then ` Returns what the <label> server answers.` and the gate sentence: for a person gate of review ` Requires user approval: PrivacyFence shows the result to the person before it is returned.`, for popup ` Requires user approval: PrivacyFence asks the person before the call is sent.`; `read_only` is verb == read and `destructive` is verb == delete) — the gate sentence never mentions auto — and update(saved).
      2. Create src/privacyfence/connectors/upstream_mcp.py: UpstreamUnavailableError, UpstreamCallRefusedError, UpstreamToolError and UpstreamMcpConnector(upstream=None, catalog=None, client=None, *, admit=None, bearer=None) (every argument optional, as the old class: copy its constructor and its `name` and `tool_specs` isinstance fallbacks from `git show f6c95e6b0:src/privacyfence/connectors/upstream_mcp.py` lines 61-77, so `cls(None)` and `cls(MagicMock())` work in connector_catalog.py, scripts/generate_tools_reference.py and the catalogue tests) with call, the two gate paths (review and popup only; both through gated_call with gate=person_gate of the saved tool, as 3.8 says) and the forward and audit helpers; the connector reads no caller context. Add tests that `UpstreamMcpConnector(None)` and `UpstreamMcpConnector(MagicMock())` have the name `upstream_mcp` and no tool specs, and run tests/unit/test_connector_catalog.py, tests/unit/test_connector_tool_annotations.py and tests/unit/test_docs_tools_reference.py.
      3. Tests: client (httpx2 transport pattern of `git show f6c95e6b0:tests/unit/test_upstream_mcp_client.py`): BearerAuth adds the header only on the matching origin and records status and insufficient_scope for both header spellings; the bearer is sent on initialize, the call and the close and no X-PrivacyFence-Claim header is ever sent (assert on every recorded request); each mapped UpstreamOAuthError kind; a 401 on initialize retries once with force_refresh True and then succeeds, a second 401 raises auth_expired; a 401 on tools/call raises auth_expired and the call is recorded exactly once; a 403 insufficient_scope; too_large and tool_error; catalog specs for a read and a write tool (schema with reason, description cut, gate sentences, read_only and destructive flags, no `auto` text). Connector: the six messages of 3.8 verbatim; admit raising CallerNotAllowedError gives UpstreamCallRefusedError and the client is never touched; a review call forwards then shows the result via gated_call, a popup call asks first and a denial never reaches the client (use the gated_call fakes of `git show f6c95e6b0:tests/unit/connectors/test_upstream_mcp_connector.py`); a tool configured `auto` still goes through gated_call with its class default gate; the audit row for a forwarded call holds the decision upstream_forwarded, metadata with upstream, gate and auth oauth, and no argument value or token.
      4. Stop condition: if the SDK's streamable_http_client does not pass the httpx2 client's auth to every request (initialize, POST, DELETE), stop with status=blocked and quote what you saw.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_client.py tests/unit/connectors/test_upstream_mcp_connector.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_client.py tests/unit/connectors/test_upstream_mcp_connector.py -q --cov=privacyfence.upstream_mcp_client --cov=privacyfence.connectors.upstream_mcp --cov-branch --cov-fail-under=100 passes
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -rn "X-PrivacyFence-Claim\|identity_claim" src/privacyfence/upstream_mcp_client.py src/privacyfence/connectors/upstream_mcp.py prints nothing
  - id: p15-runtime
    title: The runtime - configure, who sees the tools, refresh, save, status and Admin rows
    depends_on: [p11-oauth-discovery, p13-policy-hooks, p14-client-connector]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_runtime.py
      - src/privacyfence/daemon_main.py
      - tests/conftest.py
      - tests/unit/test_upstream_mcp_runtime.py
      - tests/unit/test_daemon_main.py
    brief: |
      Design sections 3.6 (flow) and 3.9 (runtime, status) of docs/1155/plan.md exactly. Read `git show f6c95e6b0:src/privacyfence/upstream_mcp_runtime.py` (configure, connectors_for_principal, status, admin_rows, _shown_url, _audit_audiences as the audit pattern) and `git show f6c95e6b0:src/privacyfence/daemon_main.py` lines 1362-1386 and 1764-1766 first.
      1. Create src/privacyfence/upstream_mcp_runtime.py (module docstring: ADR 0327 and 0329): configure, configured, reset, set_tools_changed_callback, oauth_upstream(id), upstream_ids(), registrations_for(id), tokens_for(id), connectors_for_principal, admit_caller, async refresh(upstream_id, principal) returning the pending summary or raising UpstreamRefreshError(message), save(upstream_id, gates, fetched_at), set_gates(upstream_id, gates), status, admin_rows, pending state per upstream held on the entry (not a module global). Every rule of 3.6 and 3.9 applies: the pending list is built from client.list_tools with the administrator's token path (upstream_oauth_tokens.token_path(principal, id)), classified with upstream_tools.classify and unsupported_reason, diffed against the saved list; save writes through ToolStore, calls upstream_mcp_registry.get_registry().apply, updates the connector catalog and calls the tools-changed callback; set_gates changes gates of saved tools only. The pending summary and rows follow the field lists of 3.9. refresh, save and set_gates each write one audit entry with the decisions of 3.10 (AuditEntry pattern of `_audit_audiences`; a failing write changes nothing). The two refusal messages of 3.10 and the refresh error text of 3.6 are fixed there.
      2. src/privacyfence/daemon_main.py: add `_configure_upstream_mcp()` (load_declared; None returns; configure with ToolStore(org_dir()/"upstream_tools"), registrations dir org_dir()/"upstream_oauth"; log each warning of upstream_mcp_config.leftovers(admin config) on every start, BEFORE load_declared is called (so a removed variable and a refused version 1 file are logged too); organization mode only: any other mode with a variable set raises ConfigurationError(`<variable> is set but upstream MCP servers need organization mode.`)) called where the old one was (before the web server starts); and in build_connectors extend the connector list with upstream_mcp_runtime.connectors_for_principal() when configured.
      3. tests/conftest.py: extend the autouse reset to call upstream_mcp_runtime.reset().
      4. Tests in tests/unit/test_upstream_mcp_runtime.py (fake token files via upstream_oauth_tokens.save_token under tmp_path with paths.data_dir patched as other tests do; the client is a fake with list_tools returning types.Tool objects with annotations): configure registers saved lists with no network call, raises the secret-variable error, and ignores with the 3.6 warning a saved list whose url differs from the upstream's; connectors_for_principal omits an upstream with no saved list or no token file for the person and includes it with both; admit_caller for each combination of caller type and allowed types; refresh with the administrator's token classifies read-only and other tools (defaults; a tool with readOnlyHint true and destructiveHint true is popup), offers a changed tool with its class default gate and not its saved one, diffs new, changed, removed, unsupported, keeps the pending list in memory, exposes nothing until save (connector catalog unchanged), maps not_connected and unreachable to the messages; a second refresh replaces the pending list; save without a pending list raises the 409 text and save with another fetched_at raises the second 409 text of 3.10; save applies chosen gates, defaults for unchosen tools, never saves an unsupported tool, writes the store file, registers the tables (TOOL_TO_GATE holds review or popup, never auto, UPSTREAM_AUTO_TOOLS holds the auto ones), calls the callback once and clears pending; set_gates changes a saved tool's gate, refuses an unknown tool and a bad gate with the 3.10 texts; status rows for waiting_for_admin and ready with connected true and false and the hint texts verbatim; admin_rows carry the 3.9 fields and never a token, client id or person; a restart (configure again with the same store) restores the same tables; audit rows written with counts only. tests/unit/test_daemon_main.py: the configure wiring, the organization-mode-only error, leftovers warnings logged with no variable set and with a version 1 file (which is refused after the warnings).
      5. Stop condition: lifespan is not used; if web/server.py needs to start anything for the runtime, report it as a deviation instead of editing server.py (p17 owns it).
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_runtime.py tests/unit/test_daemon_main.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_runtime.py -q --cov=privacyfence.upstream_mcp_runtime --cov-branch --cov-fail-under=100 passes
      - python3 -m pytest tests/unit -q passes
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "waiting_for_admin" src/privacyfence/upstream_mcp_runtime.py matches
  - id: p16-admin-backend
    title: The three Admin actions - refresh, save, change gates
    depends_on: [p15-runtime]
    complexity: S
    touches:
      - src/privacyfence/web/routes_admin.py
      - tests/unit/web/test_routes_admin.py
    brief: |
      Design section 3.10 (actions and errors) of docs/1155/plan.md exactly. Read web/routes_admin.py (ADMIN_ACTIONS, ADMIN_PARAMS, the POST handler, _admin_principal, _apply_step_up_gate, the audit call after an action, _admin_state) and tests/unit/web/test_routes_admin.py (ACTION_BODIES, the step-up tests) first; the example to copy end to end is set_unattended_sessions.
      1. Declare refresh_upstream_tools (not sensitive), save_upstream_tools and set_upstream_tool_gates (sensitive) with the params of 3.10 (save_upstream_tools also takes fetched_at, declared with type `float` in ADMIN_PARAMS because `_coerce` truncates an int and refuses a JSON number as str; add a test that a JSON float fetched_at survives the round trip unchanged); handle all three in the POST handler before the admin_config.apply_change dispatch by calling upstream_mcp_runtime.refresh, save and set_gates with the admin principal (refresh needs `await`); map UpstreamRefreshError and the runtime's refusals to the status codes and texts of 3.10; no ROUTE_CLASSIFICATION change is needed (the `{action}` route already covers every action); the runtime writes the audit rows (do not write a second one).
      2. Add `upstream_servers: upstream_mcp_runtime.admin_rows()` to the admin state (empty list when not configured).
      3. Tests: each action's success (the runtime faked), the sensitive ones need the step-up (428 then success, as the existing tests show), a non-admin gets 403, an unknown upstream 400, a bad gate 400 with the exact text, save without pending 409, save with a stale fetched_at 409, the refresh error text passes through, the state carries upstream_servers.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_admin.py -q passes
      - grep -n "refresh_upstream_tools\|save_upstream_tools\|set_upstream_tool_gates" src/privacyfence/web/routes_admin.py matches
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p17-connect-routes
    title: The connect and callback routes, the Connect-page rows and the server wiring
    depends_on: [p15-runtime]
    complexity: M
    touches:
      - src/privacyfence/web/routes_upstream_oauth.py
      - src/privacyfence/web/routes_connect.py
      - src/privacyfence/web/server.py
      - tests/unit/web/test_routes_upstream_oauth.py
      - tests/unit/web/test_routes_connect.py
      - tests/unit/web/test_server.py
    brief: |
      Design section 3.9 (Connect flow) of docs/1155/plan.md exactly. Read src/privacyfence/web/routes_connect.py (start, callback, _PendingAuthStore, _service_row_html, _flash_html, _render_connect_page, connect_page) and tests/unit/web/test_routes_connect.py (TestMiroOAuth, TestRelay's app helpers) first; the pattern for the callback checks is the generic callback (ADR 0178).
      1. Create src/privacyfence/web/routes_upstream_oauth.py with PendingUpstreamAuth, PendingUpstreamAuthStore, build_routes and connect_section_html as 3.9 says. It uses upstream_mcp_runtime.oauth_upstream(id), registrations_for(id) and, in connect_section_html, upstream_oauth_tokens.load_token(token_path(principal, id)) to decide Connect or Reconnect; one PendingUpstreamAuthStore is created inside build_routes; the redirect URI is issuer_url.rstrip("/") + "/upstream-mcp/<id>/callback"; HTML-escape every label; texts, redirects, no-store header, the iss rule and the logging rules are those of 3.9.
      2. routes_connect.py: read upstream_connected and upstream_error in connect_page, pass them to _render_connect_page (new keyword arguments, default ""), and place the flash and the rows as 3.9 says; the function-local import of routes_upstream_oauth avoids a cycle. Nothing else of routes_connect changes.
      3. web/server.py: mount routes_upstream_oauth.build_routes(...) right after routes_connect.build_routes inside the `if org.connector_registry is not None:` block, only when upstream_mcp_runtime.configured(); call upstream_mcp_runtime.set_tools_changed_callback(mcp_dispatcher.notify_tools_changed) when a dispatcher exists; change the token page's main tool list (`by_connector`, about lines 276-281) to include an upstream tool when it is in policy.scopes.UPSTREAM_AUTO_TOOLS (a per-tool rule can cover it) and to keep skipping the other gated upstream tools (they stay in the administrator's tick fieldset); and re-add the `page["upstreams"]` block of the token page (the helper _gated_tools stays from before p1) with entries `{"id", "label", "tools": by_connector.get(id, []), "gated_tools": _gated_tools(id) if principal.is_admin else [], "connection_note": True}` built from upstream_mcp_runtime.status() rows (nothing when not configured).
      4. Tests in tests/unit/web/test_routes_upstream_oauth.py (helpers copied in style from test_routes_connect.py; the upstream is served by an httpx2.MockTransport passed as transport, the runtime configured as the runtime tests do): the pending store is single use, expires with a fake clock and drops the oldest beyond max_entries; connect without a session redirects to /login; an unknown id gives 404; connect redirects to the authorization endpoint with state, S256 challenge, redirect_uri, client_id, scope and resource and Cache-Control no-store; a discovery failure redirects to /connect?upstream_error=<id>; callback with the right state saves the token file (mode 0600, fields of 3.5), evicts the registry entry and redirects to ?upstream_connected=<id>; callback with another principal's session, an expired state, a reused state, a state of another upstream, a provider error, a missing code, a mismatching iss, a missing iss when iss_supported, and a failing exchange all save nothing and redirect or answer as 3.9 says; the logs of these cases (caplog) contain neither the state, the code, nor a token nor secret sentinel; connect_section_html shows Connect for a person with no token and Reconnect with the Connected badge for one with a token and escapes a label such as <b>x</b>. In tests/unit/web/test_routes_connect.py add test_the_connect_page_lists_an_upstream and test_an_unknown_upstream_flash_value_is_ignored; in tests/unit/web/test_server.py a test that the token page carries the upstream entry with gated_tools for an admin and none for a non-admin, and lists an auto tool in the main tool list but a review tool only in the administrator's tick list.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_upstream_oauth.py tests/unit/web/test_routes_connect.py tests/unit/web/test_server.py -q passes
      - python3 -m pytest tests/unit/web/test_routes_upstream_oauth.py -q --cov=privacyfence.web.routes_upstream_oauth --cov-branch --cov-fail-under=100 passes
      - python3 -m pytest tests/unit -q passes
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p18-admin-ui
    title: The Admin upstream page and the token form's connection note
    depends_on: [p16-admin-backend, p17-connect-routes]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Design section 3.10 (UI) of docs/1155/plan.md exactly; every string there is literal. Read renderUpstreams (the stub left by p2), the generic click handler and saveAdminForm (about lines 2688 and 2990-2999), a form that posts a payload built from inputs (ADMIN_FORMS) and TestOrgAdminSections in tests/unit/test_settings_window_html.py first.
      1. Implement renderUpstreams(state) from state.upstream_servers (rows of 3.9): the card, the Refresh tools button (data-action refresh_upstream_tools, payload {upstream_id}; disabled with its hint when admin_connected is false), the tool table with the gate select, class badge, default marker and new/changed badge and the "Not exposed until you save." line for pending tools, the two explanatory lines, and the Save button (save_upstream_tools with payload {upstream_id, gates, fetched_at} gathered from the selects and the pending list when one exists, else set_upstream_tool_gates). A changed tool's select starts at its default (the row's `default_gate`), with the badge `changed` and the line `Its gate was reset; choose auto again if you want it.` A pending list shows the pending tools (new and changed first) with the saved gate for known tools and the default for new ones; removed tools are listed under the table as `Removed by the server: <names>`. All server text is escaped; descriptions are shown as given in the row (already cut).
      2. Token form: in the scoped-token form, for each upstream in SCOPED.upstreams that has a ticked tool (scopedUpstreamsWithTools), add `<div class="pf-hint" data-st-connection-for="<id>">This token acts through your own <label> connection: it can do what your account there can do.</div>` under that upstream's tools (labels escaped).
      3. Tests: the card for a waiting upstream, a ready one, one with a pending list (new, changed, removed, unsupported tools), the disabled refresh when the admin is not connected, the payload of each of the three actions built from the selects (parse the data-payload), escaping of a hostile label, tool name and description, the review-on-write warning, and the token-form note for an upstream with and without a ticked tool.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_window_html.py -q passes
      - grep -n "Refresh tools" src/privacyfence/settings_window_html.py matches
      - ruff check . passes
  - id: p19-reference-server
    title: A reference upstream with OAuth, for tests and a development setup
    depends_on: []
    complexity: M
    touches:
      - scripts/upstream_mcp_oauth_reference_server.py
      - tests/unit/test_upstream_mcp_oauth_reference_server.py
    brief: |
      Design section 3.11 (reference upstream) of docs/1155/plan.md exactly. Read `git show f6c95e6b0:scripts/upstream_mcp_reference_server.py` (build_app, _build_server, the session manager and lifespan wiring) and copy its way of mounting the MCP endpoint; the old file is deleted by p7, so this is a new file. Use only the standard library, starlette and mcp already installed; the module is a script (module docstring: a test aid, not production; how to run it with python3 scripts/upstream_mcp_oauth_reference_server.py --base-url http://127.0.0.1:8098 --port 8098).
      1. build_app(*, base_url, accounts=None, access_token_seconds=3600) -> Starlette with the routes, the login_hint account rule, the four tools with their annotations (types.ToolAnnotations), app.state.extra_tools and app.state.revoke_all of 3.11. State (registered clients, codes, tokens) lives in the app object, never at module level. A code is single-use and bound to client_id, redirect_uri and the PKCE challenge; refresh rotates; an expired or unknown access token on /mcp gives 401 with the WWW-Authenticate header of 3.11.
      2. main() with argparse (--base-url required, --host default 127.0.0.1, --port default 8099) running uvicorn.
      3. Tests using httpx2 against the ASGI app: both documents are served; a full dynamic registration, authorize (assert the redirect carries code, state and iss), token exchange with a correct and a wrong verifier, refresh rotation (the old refresh token no longer works), an expired token gets 401 with the header, a valid token initializes and lists the four tools through the mcp client with their annotations, whoami returns the account for no hint (alice), for a hint that is a key of accounts (the mapped name) and for a hint that is not (the hint), a reused code is refused, extra_tools appear in the next list, revoke_all makes a valid token answer 401.
      4. Stop condition: if the installed mcp lowlevel server cannot be mounted behind a bearer check the way the old script mounted it, stop with status=blocked and quote the error.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_oauth_reference_server.py -q passes
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "authorization_response_iss_parameter_supported" scripts/upstream_mcp_oauth_reference_server.py matches
  - id: p20-integration
    title: An organization server with an OAuth upstream, end to end
    depends_on: [p16-admin-backend, p17-connect-routes, p19-reference-server]
    complexity: M
    touches:
      - tests/integration/test_upstream_oauth_org.py
    brief: |
      Read `git show f6c95e6b0:tests/integration/test_upstream_mcp_org.py` (the stack fixture and helpers), docs/testing-policy.md (which tier this is) and tests/integration/test_settings_scoped_token_form_browser.py (how an Admin action is posted with step-up in tests, if at all) first. Start a real organization server on a free port in this process the way that stack fixture does, with the reference server of scripts/upstream_mcp_oauth_reference_server.py on its own free port (import it by path) and a version 2 declaration with id "oauthref", label "oauthref", the reference server's /mcp URL (http on 127.0.0.1) and no oauth keys. Because the passkey step-up cannot be answered in a test, the Admin actions are exercised in p16's unit tests and this test calls upstream_mcp_runtime.refresh, save and set_gates in process (stop with status=blocked if the server runs out of process); everything else goes over HTTP. A person is simulated with httpx2: sign in through the org server's test login the way the stack fixture does, call /upstream-mcp/oauthref/connect without following redirects, take the authorize redirect, append `&login_hint=<name>`, follow it on the reference server without following redirects, then GET the callback on the org server with the returned query.
      Tests:
      1. test_before_an_administrator_saves_a_list_the_status_says_waiting_for_admin_and_no_tool_is_listed.
      2. test_refresh_lists_defaults_and_exposes_nothing_until_save: after connecting as alice (the administrator) and refreshing, whoami and echo are pending with gate review, rename_thing and wipe_thing popup, tools/list through /mcp still has no oauthref tool; after save (whoami set to auto) tools/list has the four tools.
      3. test_a_person_calls_a_review_tool_as_themselves: connect as bob, call oauthref_echo, answer the review card through the approval API the stack fixture uses, and call oauthref_whoami to get bob.
      4. test_always_allow_for_an_auto_tool_removes_the_card: call oauthref_whoami (auto ceiling, class default review) as bob, press Always allow through the approval API (the proposal list for the call must contain one proposal), call it again and get the result with no card; oauthref_echo, configured review, offers no proposal.
      5. test_lowering_the_ceiling_makes_the_stored_rule_inert: set_gates whoami to review, call it as bob: a card is shown again.
      6. test_a_changed_tool_is_held_until_save_and_its_ceiling_is_reset: app.state.extra_tools gets a new tool, refresh shows it new, tools/list does not include it until save; a refresh after the reference server changes whoami's description marks whoami changed with its class default gate, and saving without choosing auto lowers it (a stored Always allow rule of bob's no longer removes the card); a save carrying a stale fetched_at is refused.
      7. test_a_scoped_token_needs_an_approval_or_a_rule: creating a token that lists oauthref_wipe_thing with no approval and no rule is refused with the text of 3.7 (as the administrator and as a non-administrator); with the administrator's tick (digest from UPSTREAM_DECLARATIONS) it is created and the call goes through; a non-administrator can create a token for oauthref_whoami (set to auto) carrying the per-tool rule and the call goes through with no card, and after the ceiling is lowered the same token's call is denied.
      8. test_a_revoked_connection_gives_the_expired_message: app.state.revoke_all(), call a tool as bob, expect a message starting "The oauthref sign-in has expired or was revoked.", and the token file removed.
      Keep it under 90 seconds in total. Touch no other file.
    acceptance:
      - python3 -m pytest tests/integration/test_upstream_oauth_org.py -q passes
      - python3 -m pytest tests/integration -q passes
      - ruff check . passes
  - id: p21-adrs
    title: ADRs 0327-0329 and the ADR index
    depends_on: [p18-admin-ui, p20-integration]
    complexity: S
    touches:
      - docs/adr/0327-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md
      - docs/adr/0328-oauth-upstreams-discover-their-server-register-a-client-and-keep-tokens-per-person.md
      - docs/adr/0329-the-administrator-manages-each-upstream-servers-tools.md
      - docs/adr/README.md
    brief: |
      Write the three ADRs from design sections 3.1-3.11 and 4 of docs/1155/plan.md, using what the code now does (read src/privacyfence/upstream_mcp_config.py, upstream_oauth_discovery.py, upstream_oauth_tokens.py, upstream_tools.py, upstream_mcp_registry.py, upstream_mcp_runtime.py and web/routes_upstream_oauth.py to quote real names). Pattern commit: 0f4910958 (ADRs 0325 and 0326). Read the template section of docs/adr/README.md first.
      1. Each ADR has Status (Accepted — 2026-11-19, Implemented, and the Supersedes, Amends and Relates lines of section 4), Context (issue #1155 link), Decision, Alternatives considered (from 3.11), Consequences, Verification (the test files of p1-p20), Related. ADR 0327 records the breaking removal and what it leaves on disk and in the audit log, the version 2 file, no claim, the principal-type check, the scoped token acting through its creator's connection, and the owner decisions on no Disconnect; ADR 0328 records the DNS-rebinding residual risk, the left-out metadata-document method and that the registration cache records the method; ADR 0329 records the untrusted-annotation default rule and its residual, that Refresh reads a server-wide list with the administrator's own connection, the reset of a changed tool's gate, the `fetched_at` binding of Save, the hold, the ceiling semantics, the live registration and its lock, the per-tool rule and the digest.
      2. docs/adr/README.md: three index rows in the existing format, and change the rows of 0285, 0286, 0287, 0288, 0297, 0298, 0299, 0310, 0325 and 0326 to end `Superseded by 0327`, the row of 0319 to `Superseded by 0329` and the row of 0300 to add `amended by 0329` (keep each row's earlier text).
    acceptance:
      - ls docs/adr/0327-*.md docs/adr/0328-*.md docs/adr/0329-*.md lists three files
      - grep -c "Superseded by 0327" docs/adr/README.md prints 10
      - python3 -m pytest tests/unit/test_docs_references_exist.py -q passes
  - id: p22-adr-status
    title: Mark the superseded and amended ADRs
    depends_on: [p21-adrs]
    complexity: M
    touches:
      - docs/adr/0285-upstream-mcp-servers-are-declared-connectors.md
      - docs/adr/0286-privacyfence-sends-the-upstream-a-signed-caller-claim.md
      - docs/adr/0287-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md
      - docs/adr/0288-the-upstream-mcp-declaration-can-be-yaml-or-inline-and-seeds-the-grants.md
      - docs/adr/0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md
      - docs/adr/0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md
      - docs/adr/0299-group-membership-is-updated-through-a-scoped-token-tool.md
      - docs/adr/0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md
      - docs/adr/0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md
      - docs/adr/0319-the-admin-page-shows-upstream-mcp-servers-read-only.md
      - docs/adr/0325-an-upstream-can-be-the-source-of-its-own-audiences.md
      - docs/adr/0326-a-service-claim-may-call-the-declared-audiences-tool.md
    brief: |
      Literal edits only, in the repository's convention (see the last line of the Status block of docs/adr/0057-*.md: a bold line `**Superseded by [ADR NNNN](file.md), YYYY-MM-DD.**`). In each file's `## Status` block add, after the existing status text and a blank line: for 0285, 0286, 0287, 0288, 0297, 0298, 0299, 0310, 0325 and 0326 `**Superseded by [ADR 0327](0327-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md), 2026-11-19.**`; for 0319 `**Superseded by [ADR 0329](0329-the-administrator-manages-each-upstream-servers-tools.md), 2026-11-19.**`; for 0300 `**Amended by [ADR 0329](0329-the-administrator-manages-each-upstream-servers-tools.md), 2026-11-19.**` (leave the file's existing `Accepted` text and any earlier "Amended by" lines as they are). Do not edit any other part of an accepted ADR.
    acceptance:
      - grep -l "Superseded by \[ADR 0327\]" docs/adr/0285-*.md docs/adr/0286-*.md docs/adr/0287-*.md docs/adr/0288-*.md docs/adr/0297-*.md docs/adr/0298-*.md docs/adr/0299-*.md docs/adr/0310-*.md docs/adr/0325-*.md docs/adr/0326-*.md lists all ten files
      - grep -n "Superseded by \[ADR 0329\]" docs/adr/0319-the-admin-page-shows-upstream-mcp-servers-read-only.md matches
      - python3 -m pytest tests/unit/test_docs_references_exist.py -q passes
  - id: p23-docs
    title: The new upstream docs and the changelog entry
    depends_on: [p22-adr-status]
    complexity: M
    touches:
      - docs/upstream-mcp.md
      - docs/identity-claim.md
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - docs/configuration-reference.md
      - docs/connecting-a-service.md
      - docs/org-mode-docker.md
      - docs/connector-qa.md
      - docs/README.md
      - CHANGELOG.md
    brief: |
      Write the reference docs from design sections 3.1-3.10 and 5-6 of docs/1155/plan.md and from what the code now does (read src/privacyfence/upstream_mcp_config.py, web/routes_upstream_oauth.py and upstream_mcp_runtime.py to quote real names). Pattern commit: 0f4910958.
      1. Replace the tombstone docs/upstream-mcp.md (p8) with the new document (starts with `# Upstream MCP servers behind PrivacyFence` and a summary of at most 3 lines; at most 400 lines): for operators (the version 2 file with a Canva and a Framer example in YAML, the keys table of 3.2, restart), for administrators (connect, Refresh tools, the default-gate rule and why annotations are only a default, hold until Save, review/popup/auto with auto as a ceiling, per-tool Always allow, scoped tokens and the ADR 0300 tick and per-tool token rules), for people (Connect, the messages of 3.8), `privacyfence_status` fields, errors and size, audit (`upstream_forwarded`, `upstream_tools_refreshed`, `upstream_tools_saved`, `upstream_tool_gates_changed`), cautions (declare `oauth.scopes`; a reverse proxy that logs the callback query; the client secret variable; a server that marks a write tool read-only), leftovers of the removed feature and how to clean them up (manual step 3 of the plan), and the reference server for development; and update docs/identity-claim.md's tombstone sentence to link ADR 0327 now that it exists.
      2. docs/configuration-reference.md: two rows for PRIVACYFENCE_UPSTREAM_MCP_FILE and PRIVACYFENCE_UPSTREAM_MCP (version 2 file, organization mode only, never both, at most 256 KiB, restart needed, link to docs/upstream-mcp.md) in the same table and style as the deleted rows; tests/unit/test_docs_configuration_reference.py must pass. docs/connecting-a-service.md: one paragraph that a declared upstream appears on the Connect page. docs/org-mode-docker.md: a short "Upstream MCP file" section with one inline example and the secret-variable note. docs/connector-qa.md: the two manual QA checks of section 5 (Canva, Framer) as a short subsection. docs/README.md: one row for docs/upstream-mcp.md in the existing row format.
      3. scripts/generate_tools_reference.py (about lines 234-236): replace the sentence that says an upstream tool's gate is the one the operator declared by `Their gate is set by an administrator on the Admin page; a tool the administrator set to auto still asks until you make an Always allow rule for it.`, then regenerate docs/tools-reference.md with the script so tests/unit/test_docs_tools_reference.py passes.
      4. CHANGELOG.md under `## [Unreleased]` (never a version heading): "- **Hosted MCP servers with their own sign-in.** It replaces the claim-based upstream servers of the 8.0 alphas, which are removed. An operator can declare an upstream (`id`, `label`, `url`, optional `oauth` settings, for example Canva or Framer) and each person connects their own account on the Connect page. An administrator refreshes the server's tool list on the Admin page, reviews the defaults (read-only tools `review`, everything else `popup`) and saves; new or changed tools stay hidden until then. A tool set to `auto` keeps asking until a person makes an Always allow rule for it ([ADR 0327](docs/adr/0327-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md), [ADR 0329](docs/adr/0329-the-administrator-manages-each-upstream-servers-tools.md), [#1155](https://github.com/privacyfence/privacyfence/issues/1155))."
    acceptance:
      - python3 -m pytest tests/unit/test_docs_configuration_reference.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_links.py -q passes
      - grep -n "Refresh tools" docs/upstream-mcp.md matches
      - awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep -c "Hosted MCP servers with their own sign-in" prints 1
      - ruff check . passes
```
