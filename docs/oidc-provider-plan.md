# PrivacyFence as the sign-in provider for Peek and Grist

## Goal

An organization server, or a single-user install on `localhost`, can be the OpenID Connect sign-in
provider for a few web apps the operator names, such as Peek and Grist in the `privacyfence-suite`
Docker stack. People sign in to Peek and Grist with **Sign in with SSO**, which goes to
PrivacyFence. Peek and Grist then get the same identity PrivacyFence has from its own sign-in:

- the organization's identity provider;
- the built-in Google sign-in;
- or the passkey on `localhost`.

Nothing is ever registered with the identity provider, or with Google, for Peek or Grist. The
operator names each app, its redirect address and its secret in PrivacyFence's environment, so the
suite can generate them.

## Current state

- **PrivacyFence's OAuth server serves only MCP clients** (ADR 0011).
  - The provider is `OrgOAuthProvider` in `src/privacyfence/web/oauth_provider.py`; its routes are
    built by `mount_org_oauth` (`src/privacyfence/web/routes_mcp.py:1052-1163`).
  - Endpoints: `/.well-known/oauth-authorization-server`, `/authorize`, `/token`, `/register` and
    `/revoke`.
  - Tokens are opaque. There is no ID token, no JWKS, no userinfo and no
    `/.well-known/openid-configuration` anywhere in `src/`, and nothing uses the `/oidc` prefix.
  - `org_identity.py` is the *client* side: it verifies the upstream IdP's ID tokens with PyJWT
    (`org_identity.py:35-37`).
- **Browser sessions** (`src/privacyfence/web/org_session.py`).
  - `OrgSession(principal, created_at, last_seen_at)` (`:50`).
  - `OrgSessionStore.create(principal) -> str` (`:75`) and `get(session_id) -> Principal | None`
    (`:82`). The session cookie is `pf_org_session`, host-only, path `/`, SameSite=Lax (`:130-161`).
  - The IdP sign-in creates the session at `routes_org_identity.py:273`, with `claims` in scope. The
    single-user sign-in creates it at `single_user_auth.py:301`, for the fixed `SINGLE_USER_PRINCIPAL`
    (`admin@localhost`).
- **`email_verified` is not recorded for the session.**
  - `peek_provisioning.email_verified_claim` (`peek_provisioning.py:333-335`) and the Grist copy
    (`grist_provisioning.py:387-391`) only read the claim (`value is True or value == "true"`).
  - The "trust unverified email" flag lives on the sign-in policy and is used only by
    `check_authz_policy`'s allowed-domain branch (`org_identity.py:466-476`; field comment at
    `org_identity.py:103-105`).
- **The sign-in redirect keeps the query string.**
  - `/login?next=<local path>` signs a browser in, in IdP mode (handler at
    `routes_org_identity.py:226`) and in single-user mode (`single_user_auth.py:361-365`, and the
    setup-code form at `:390-392`).
  - `safe_next_path` (`routes_security.py:151-158`, `routes_org_identity.py:138-156`) keeps the query
    string. It refuses only a path that does not start with `/`, starts with `//` or `/\`, or contains
    whitespace or control characters.
- **App wiring.**
  - `OrgAuth` (`src/privacyfence/web/server.py:698-751`, `frozen=True`) carries the org-mode pieces to
    `_build_org_app` (`server.py:1326`), which mounts `mount_org_oauth` at `server.py:1374`.
  - `daemon_main.py:1484` builds the `OrgAuth`. In org mode, with `web.mcp.enabled` false, there is no
    web server (`daemon_main.py:988-990`).
  - The org app's middleware stack is `_SecurityHeadersMiddleware`, `_HostAllowlistMiddleware` and
    `_PrincipalScopeMiddleware` (`server.py:1496-1499`). There is no global Origin or CSRF check:
    routes call `check_origin` and `check_csrf` themselves.
  - The Host allowlist (`server.py:1682-1692`, `:925-928`) accepts the issuer's host, `127.0.0.1` and
    `::1`.
- **Tests.** `tests/conftest.py:177-189` clears the server's environment variables, and
  `:212-232` redirects org-directory writes to `tmp_path`.
- **Libraries.** `PyJWT` and `cryptography>=50.0.0` are already dependencies.
- **Constraining ADRs:**
  - 0011: org mode runs its own OAuth server.
  - 0013: no MCP tool may mint a sign-in credential. Holds: the ID token comes from an HTTP route.
  - 0061: MCP tokens and browser sessions are audience-separated. Holds: separate token stores.
  - 0177: MCP consent.
  - 0224: registration attested by redirect URIs.
  - 0256/0264: startup settings come from the environment.

## Design

### Clients from the environment (`src/privacyfence/oidc_provider.py`, new)

```
PRIVACYFENCE_OIDC_CLIENTS=peek,grist
PRIVACYFENCE_OIDC_CLIENT_PEEK_REDIRECT_URIS=https://peek.example.com/oauth/oidc/callback
PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET_FILE=/run/suite/peek_oidc_client_secret
PRIVACYFENCE_OIDC_CLIENT_GRIST_REDIRECT_URIS=https://grist.example.com/oauth2/callback
PRIVACYFENCE_OIDC_CLIENT_GRIST_SECRET_FILE=/run/suite/grist_oidc_client_secret
```

- **Names.** Each name in `PRIVACYFENCE_OIDC_CLIENTS` is comma-separated, trimmed and lower-cased. It
  must match `[a-z][a-z0-9-]{0,31}`, and it is the `client_id`.
- **Variable stem.** Each client's variables start with
  `PRIVACYFENCE_OIDC_CLIENT_<NAME upper-cased, "-" → "_">_`.
- **Redirect URIs.** `…_REDIRECT_URIS` is comma-separated and needs at least one entry. Each must be
  `https://`, or `http://` with host `localhost`, `127.0.0.1` or `::1`, with no userinfo and no
  fragment. They are compared exactly.
- **Secret.** `…_SECRET_FILE` or `…_SECRET` holds the client secret. Reading it copies
  `org_mode.provisioning_key` (`org_mode.py:158-193`): it catches `(OSError, UnicodeDecodeError)` and
  never quotes the reason. After `strip()` it must hold at least 32 characters.
- **Exact `ConfigurationError` messages:**
  - `PRIVACYFENCE_OIDC_CLIENTS has an invalid name: <name>`
  - `<STEM>REDIRECT_URIS must list at least one address`
  - `<STEM>REDIRECT_URIS has an address that is not https:// (http:// only for localhost) or has a user name or fragment: <uri>`
  - `Set <STEM>SECRET or <STEM>SECRET_FILE, not both.`
  - `<STEM>SECRET_FILE could not be read.`
  - `<STEM>SECRET must be at least 32 characters`
  - `PRIVACYFENCE_OIDC_CLIENTS is set, but this is not an organization server.`
- **Data types:**
  - `@dataclass(frozen=True) class OidcClient: client_id: str; redirect_uris: tuple[str, ...]; secret: str = field(repr=False)`.
  - `def clients_from_env() -> dict[str, OidcClient]` returns `{}` when the variable is unset or empty.

### Signing key

- **Generation.** RSA 2048, generated on first start with `cryptography`. It is written atomically
  (temp file in the same directory, mode `0600`, then `os.replace`) to the path argument of
  `load_or_create_signing_key(path: Path) -> SigningKey`. The daemon passes
  `paths.org_dir() / "oidc_signing_key.pem"` (PKCS8 PEM).
- **A file that exists but is unusable** (cannot be read, is not PEM, is not RSA, or is under 2048
  bits) refuses the start with
  `ConfigurationError("<path> is not a usable RSA signing key of at least 2048 bits; delete it to make a new one.")`.
- **`kid`** is the first 16 characters of the base64url (unpadded) SHA-256 of the DER
  SubjectPublicKeyInfo.
- **RS256**, because `openid-client` (Grist) defaults to it and `go-oidc` (Peek) accepts it.
- **Types.** `@dataclass(frozen=True) class SigningKey: private_key: RSAPrivateKey = field(repr=False); kid: str`.
  `jwks(key) -> dict` returns
  `{"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid", "n", "e"}]}`.
- **Rotation.** Deleting the file rotates the key at the next start. ID tokens live only 5 minutes.

### Provider state (`OidcProvider`, in `oidc_provider.py`)

```python
@dataclass(frozen=True)
class CodeGrant:
    client_id: str
    redirect_uri: str
    principal: Principal
    email_verified: bool
    nonce: str | None
    code_challenge: str
    scope: str            # the requested scopes, space-separated, "openid" always included
    auth_time: int
    expires_at: float

class OidcProvider:
    def __init__(self, *, issuer: str, clients: dict[str, OidcClient], key: SigningKey,
                 clock: Callable[[], float] = time.time) -> None: ...
    issuer: str
    def discovery(self) -> dict[str, object]: ...
    def jwks(self) -> dict[str, object]: ...
    def client(self, client_id: str) -> OidcClient | None: ...
    def authenticate(self, client_id: str, secret: str) -> OidcClient | None: ...
    def issue_code(self, *, client_id: str, redirect_uri: str, principal: Principal, email_verified: bool,
                   nonce: str | None, code_challenge: str, scope: str, auth_time: int) -> str | None: ...
    def redeem_code(self, code: str, *, client_id: str, redirect_uri: str, code_verifier: str) -> CodeGrant | None: ...
    def issue_tokens(self, grant: CodeGrant) -> dict[str, object]: ...
    def userinfo(self, access_token: str) -> dict[str, object] | None: ...
```

- **State.** Codes and access tokens are two dicts guarded by one `threading.Lock`. There is no
  module-level state, so `tests/conftest.py` needs no reset.
- **Pruning and caps.** Every insert first prunes expired entries. If more than 10,000 live codes
  remain, `issue_code` returns None; the route then answers with `temporarily_unavailable`. Access
  tokens have the same cap, and `issue_tokens` drops the oldest token when it is reached.
- **`authenticate`** compares `secret.encode()` with the client secret's bytes using
  `hmac.compare_digest`, which never raises on non-ASCII input.
- **`redeem_code`** pops the code (single use). It returns the grant only when all of these hold:
  - the code is unexpired (60 s) and belongs to `client_id`;
  - `redirect_uri` is the same;
  - `base64url(sha256(code_verifier.encode()))`, unpadded, equals `code_challenge`.
- **`issue_tokens`** answers the token response:
  `{"access_token": token_urlsafe(32), "token_type": "Bearer", "expires_in": 300, "scope": grant.scope, "id_token": <jwt>}`.
  - The ID token claims are:
    - `iss`, `sub` (principal id) and `aud` (client_id);
    - `iat`, and `exp` = `iat`+300;
    - `auth_time`, and `nonce` when given;
    - `at_hash`: the left half of the SHA-256 of the access token, base64url unpadded;
    - `email`, `email_verified`, and `name` (the display name).
  - It is signed RS256 with header `kid`.
  - The access token is stored for 300 s with the grant's `sub`, `email`, `email_verified` and `name`.
- **`userinfo`** returns `{"sub", "email", "email_verified", "name"}` for a live token, else None.

### Endpoints (`src/privacyfence/web/routes_oidc.py`, new)

The issuer is `issuer_url.rstrip("/") + "/oidc"`, where `issuer_url` is the server's
`PRIVACYFENCE_ISSUER_URL`, or `http://localhost:<port>` in single-user mode. Every route sits under
it, apart from the MCP authorization server's metadata.

**`GET /oidc/.well-known/openid-configuration`** (no auth) returns:

```json
{"issuer": "<iss>", "authorization_endpoint": "<iss>/authorize", "token_endpoint": "<iss>/token",
 "userinfo_endpoint": "<iss>/userinfo", "jwks_uri": "<iss>/jwks",
 "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
 "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
 "scopes_supported": ["openid", "email", "profile"],
 "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
 "code_challenge_methods_supported": ["S256"],
 "claims_supported": ["sub", "email", "email_verified", "name"]}
```

There is no `end_session_endpoint`. The apps sign out locally; Grist needs
`GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT=true`.

**`GET /oidc/jwks`** (no auth) returns `provider.jwks()`.

**`GET /oidc/authorize`.** Only GET is supported, deliberately, since both clients use it. The steps
run in this order:

1. **Client and redirect URI, never redirected.**
   - An unknown `client_id` gets plain-text 400 `Unknown client.`.
   - A `redirect_uri` that is not one of the client's gets plain-text 400
     `This redirect address is not registered for this client.`.
2. **Request checks, answered by an error redirect.** These redirect with `error` and
   `error_description`:
   - `response_type` other than `code` gives `unsupported_response_type`.
   - `scope` without `openid` gives `invalid_scope`.
   - A missing `code_challenge`, a method other than `S256`, or a challenge that does not match
     `[A-Za-z0-9._~-]{43,128}` gives `invalid_request` with `PKCE with S256 is required.`. PKCE is
     mandatory.

   The error redirect is built with `urllib.parse.urlencode`. It is appended with `?`, or with `&`
   when the registered URI already has a query. It carries `state` only when the request had one.
   `state` is optional.
3. **No session: send the browser to sign in.** With no valid browser session (from the
   `pf_org_session` cookie via `sessions.get_session`), answer 302 to
   `/login?next=<urllib.parse.quote(path + "?" + query, safe="")>`.
4. **No email.** A principal with no email gets error redirect `access_denied`.
5. **Issue the code.** Call `issue_code` with `auth_time=int(session.created_at)` and the requested
   scopes. On None, error redirect `temporarily_unavailable`. Otherwise answer 302 to
   `redirect_uri` + (`?` or `&`) + `urlencode({"code": code, "state": state})` (`state` only when
   given), with `Cache-Control: no-store`.

There is no consent page, and `prompt` and `max_age` are ignored (ADR below).

**`POST /oidc/token`** (form body, JSON answers, `Cache-Control: no-store`):

- **Client authentication.**
  - `Authorization: Basic` is decoded with `urllib.parse.unquote_plus` on each part (openid-client
    form-encodes them), in the style of `routes_mcp._client_id_from_basic` (`routes_mcp.py:966-981`).
  - Otherwise the `client_id` and `client_secret` form fields are used.
  - Basic and a form `client_secret` together give 400 `{"error": "invalid_request"}`.
  - A failure gives 401 `{"error": "invalid_client"}` with `WWW-Authenticate: Basic`.
- **Grant.**
  - A `grant_type` other than `authorization_code` gives 400 `{"error": "unsupported_grant_type"}`.
  - When `redeem_code(code, client_id=…, redirect_uri=…, code_verifier=…)` returns None, the answer is
    400 `{"error": "invalid_grant"}`. A missing `code_verifier` counts as a mismatch.
- **Success** answers `provider.issue_tokens(grant)`, and logs at INFO in `routes_oidc.py`:
  `OIDC sign-in to <client_id> for principal <principal id>`. It never logs an email, code or token.

**`GET` or `POST /oidc/userinfo`** needs `Authorization: Bearer <access_token>`. It answers
`provider.userinfo(token)`. Otherwise it answers 401 `{"error": "invalid_token"}` with
`WWW-Authenticate: Bearer error="invalid_token"`.

`/oidc/token` and `/oidc/userinfo` need no Origin exemption, since no Origin middleware exists. An
`/oidc` access token is not an MCP token, and the reverse is also true: the stores are separate (ADR
0061).

### `email_verified` for the session

- **Session store changes.**
  - `OrgSession` gains `email_verified: bool = False`.
  - `OrgSessionStore.create(principal, *, email_verified: bool = False)`.
  - A new `OrgSessionStore.get_session(session_id) -> OrgSession | None` keeps `get`'s expiry and
    last-seen behaviour, and `get` becomes `get_session(...).principal` (or None).
- **New rule** in `org_identity.py`: `session_email_verified(claims, principal, policy) -> bool` is
  true when:
  - `claims.get("email_verified") is True` or the claim is `"true"`; or
  - the policy is enabled, `policy.trust_unverified_email` is true, and the principal's email domain
    is in `policy.allowed_domains`.

  This is the only case in which the flag means anything today: `check_authz_policy`,
  `org_identity.py:466-476`. Only an address the IdP verified, or an operator-trusted address in an
  allowed domain, is passed on as verified, because Peek and Grist link accounts by email.
- **IdP sign-in** (`routes_org_identity.py:273`) passes
  `email_verified=org_identity.session_email_verified(claims, principal, policy)`. This includes the
  built-in Google sign-in, whose admission already requires the claim.
- **Single-user sign-in** (`single_user_auth.py:301`) passes `email_verified=True`. The address is
  fixed in code, and the install has exactly one person.
- **Unverified sessions.** Peek and Grist refuse a sign-in whose `email_verified` is false. That is
  intended, and is the same bar as automatic accounts (ADRs 0243, 0247).

### Wiring

- **`OrgAuth`** gains `oidc: OidcProvider | None = None`, with the type imported under
  `TYPE_CHECKING` as for `PushNotifier`.
- **Local mode.** `daemon_main` checks right after `mode = org_mode.resolve_mode(org_config)`
  (`daemon_main.py:972`): if the mode is not `"org"` and `clients_from_env()` is non-empty, it raises
  the local-mode `ConfigurationError`.
- **Org mode without a web server.** With `web.mcp.enabled` false, it logs
  `PRIVACYFENCE_OIDC_CLIENTS is ignored: the web server is off.` at WARNING.
- **Building the provider.** At the `OrgAuth(...)` call (`daemon_main.py:1484`), when the clients are
  non-empty, the daemon builds
  `OidcProvider(issuer=server_config.issuer_url.rstrip("/") + "/oidc", clients=…, key=load_or_create_signing_key(paths.org_dir() / "oidc_signing_key.pem"))`
  and logs `OIDC provider for <n> app(s) at <issuer>`.
- **Routes.** `_build_org_app` mounts `routes_oidc.build_routes(org.oidc, sessions=org.sessions)`
  right after `mount_org_oauth` (`server.py:1374`) when `org.oidc` is set.

### Single-user mode and the network

The single-user issuer is `http://localhost:<port>/oidc`. Peek and Grist fetch the discovery
document, `/oidc/token`, `/oidc/jwks` and `/oidc/userinfo` server-side, and both require `iss` to
equal that string exactly. So they must reach PrivacyFence at `localhost:<port>` from wherever they
run. The suite's localhost stack runs Peek, Grist and PrivacyFence in one shared network namespace,
so this holds. A deployment with separate containers and separate network namespaces cannot use
single-user OIDC; the docs say so.

**Cookies on localhost.** Browsers do not separate cookies by port, so `pf_org_session` is also sent
to Peek and Grist on other `localhost` ports. It is HttpOnly, and PrivacyFence's own POST routes check
`Origin` (`localhost:7700` is not `localhost:8765`). The exposure is still recorded in ADR 0273 and
`docs/oidc-provider.md`, for single-user installs on one machine only.

### Rejected

- **Reusing the MCP authorization server's `/authorize` and `/token`.** They are built by the MCP SDK
  for dynamically registered clients, with consent and opaque tokens, and are audience-separated from
  browser sessions (ADR 0061). An OIDC flow for first-party web apps has different clients, no
  consent and an ID token. Mixing them would weaken both.
- **Dynamic client registration for OIDC clients.** Any page could register itself and sign people in
  silently. Only the operator names apps.
- **A consent page.** The operator named the app; asking every person "allow Peek?" adds a click and
  protects nothing. Without consent, any site can make a signed-in browser mint a code. But the code
  only ever goes to an exactly matched registered address, in the person's own browser, and is
  useless without that client's PKCE verifier and state. What remains is a "signed in to
  PrivacyFence?" signal, which ADR 0273 records.
- **Optional PKCE.** Both clients send S256, so a no-PKCE path would only keep code injection
  possible.
- **Refresh tokens and long-lived access tokens.** Peek and Grist keep their own sessions and need the
  ID token or userinfo only at sign-in.
- **ES256 or EdDSA.** RS256 is the one algorithm every OIDC client library accepts by default.
- **Relaying the upstream IdP's ID token.** It names PrivacyFence's client as its audience, and it
  does not exist in single-user or built-in-Google mode.
- **`prompt`, `max_age` and POST to the authorization endpoint.** The two clients use none of them.

## ADRs

- **0273**: PrivacyFence is an OpenID Connect provider for web apps the operator names in the
  environment, under `<issuer>/oidc`, apart from the MCP authorization server. It has no consent page,
  no dynamic registration and no refresh tokens, and PKCE S256 is mandatory. `email_verified` follows
  `session_email_verified`. The ADR also records the cookie exposure on single-user `localhost` and
  the "signed in?" signal.
- **0274**: The OIDC signing key is an RS256 key generated in the org directory; deleting it rotates
  it.

The numbers assume `plan/env-automatic-accounts` takes 0271 and 0272. If either plan lands first with
other numbers, the retirement phase takes the next free numbers.

## Manual steps

None. The suite plan's `manual_after` signs in to Peek and Grist through PrivacyFence for real, and
its localhost end-to-end test scripts the same flow in CI.

## Risks and open questions

- **Merge conflicts with `plan/env-automatic-accounts`.** That plan also edits
  `single_user_auth.py`'s `start_session` (`:300-304`) and `tests/unit/test_daemon_main.py`. If it
  merged first, rebase and keep both changes. If a conflict is not mechanical, stop with
  `status=blocked`.
- **The Host allowlist.** Peek and Grist call `/oidc/token` through the proxy with the issuer's host,
  or through `localhost` in the suite's localhost stack. A full-app test
  (`tests/unit/web/test_server_org_mode.py`) proves that a request with the issuer's Host and no
  `Origin` reaches `/oidc/token`. If it gets 400 "Invalid Host header" or 403, stop with
  `status=blocked`.
- **`OrgAuth` is frozen.** If adding the field breaks an existing positional construction, stop with
  `status=blocked` rather than reorder fields.

## Implementation manifest

```yaml
plan_slug: oidc-provider
feature_branch: feature/oidc-provider
max_parallel: 2
verify_after_merge:
  - python3 -m pytest tests/unit/test_oidc_provider.py tests/unit/web/test_routes_oidc.py tests/unit/web/test_org_session.py tests/unit/web/test_routes_org_identity.py tests/unit/web/test_single_user_auth.py tests/unit/test_org_identity.py tests/unit/web/test_server_org_mode.py -q
final_checks:
  - docs/oidc-provider-plan.md is deleted and nothing links to it
  - the two ADRs from the ADRs section exist, are Accepted, and are listed in docs/adr/README.md
  - CHANGELOG.md has the OIDC provider entry under "## [Unreleased]" and no new version heading
phases:
  - id: p1-clients-and-keys
    title: OIDC clients from the environment, the signing key, discovery and JWKS
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/oidc_provider.py
      - src/privacyfence/web/routes_oidc.py
      - src/privacyfence/web/server.py
      - src/privacyfence/daemon_main.py
      - tests/conftest.py
      - tests/unit/test_oidc_provider.py
      - tests/unit/web/test_routes_oidc.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read docs/oidc-provider-plan.md: "Clients from the environment", "Signing key", the class
      skeleton in "Provider state" (only __init__, issuer, discovery, jwks, client in this phase),
      the discovery and JWKS parts of "Endpoints", and "Wiring".
      1. New src/privacyfence/oidc_provider.py: OidcClient, clients_from_env() with every message,
         SigningKey, load_or_create_signing_key(path), the module-level function jwks(key), and
         OidcProvider with __init__, issuer, discovery(), jwks() and client(). Follow §1.2 of
         docs/coding-and-testing-guidelines.md for the module docstring.
      2. New src/privacyfence/web/routes_oidc.py: build_routes(provider, *, sessions) -> list[Route]
         with the discovery and JWKS routes only.
      3. server.py: add the oidc field to OrgAuth (TYPE_CHECKING import, a short comment like its
         neighbours), and mount the routes after mount_org_oauth when org.oidc is set. daemon_main.py:
         the local-mode check after resolve_mode, the web-server-off warning, and the provider build
         and start log at the OrgAuth(...) call, exactly as in "Wiring". Stop condition: see Risks
         (OrgAuth is frozen).
      4. tests/conftest.py, _clear_server_environment (line 177): also clear every variable whose name
         starts with PRIVACYFENCE_OIDC_, the same way as the PRIVACYFENCE_IDP_ prefix.
      5. Tests:
         - tests/unit/test_oidc_provider.py: every message; two clients parsed; the secret is not in
           repr; the key is created once with mode 0600 and reused; an unusable file raises the
           message; the kid is stable; the JWKS n/e match the key. Use tmp_path for the key path.
         - tests/unit/web/test_routes_oidc.py: the discovery JSON exactly, including an issuer built
           from a URL with a trailing slash; a token signed with the key verifies with PyJWT against
           the JWKS.
         - tests/unit/test_daemon_main.py: the provider is built when the variable is set and absent
           otherwise; the local-mode error; the web-server-off warning. Patch
           oidc_provider.load_or_create_signing_key or paths.org_dir to tmp_path, so nothing is
           written into the checkout.
      6. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`,
         `python3 scripts/mypy_strict_modules.py` and `bandit -c pyproject.toml -r src`.
    acceptance:
      - python3 -m pytest tests/unit/test_oidc_provider.py tests/unit/web/test_routes_oidc.py tests/unit/test_daemon_main.py -q passes
      - grep -n "def clients_from_env" src/privacyfence/oidc_provider.py finds it
      - grep -n "PRIVACYFENCE_OIDC_" tests/conftest.py finds the prefix clear
      - ruff check . and bandit -c pyproject.toml -r src pass

  - id: p2-session-email-verified
    title: Browser sessions remember whether the person's email is verified
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/org_identity.py
      - src/privacyfence/web/org_session.py
      - src/privacyfence/web/routes_org_identity.py
      - src/privacyfence/web/single_user_auth.py
      - tests/unit/test_org_identity.py
      - tests/unit/web/test_org_session.py
      - tests/unit/web/test_routes_org_identity.py
      - tests/unit/web/test_single_user_auth.py
    brief: |
      Read docs/oidc-provider-plan.md, section "email_verified for the session".
      1. org_identity.py: add session_email_verified(claims, principal, policy) -> bool, next to
         check_authz_policy, exactly as the rule says. Read the policy fields the way
         check_authz_policy does (getattr(policy, "trust_unverified_email", False) is True, and
         policy.allowed_domains; email and domain helpers _normalized_email and _email_domain).
      2. org_session.py: add email_verified to OrgSession (default False), the keyword argument to
         create(), and get_session(). Rewrite get() on top of get_session() without changing its
         behaviour.
      3. routes_org_identity.py:273: pass
         email_verified=org_identity.session_email_verified(claims, principal, policy).
      4. single_user_auth.py:301: pass email_verified=True, with a one-line comment (fixed address,
         one person).
      5. Tests:
         - tests/unit/test_org_identity.py: claim True; claim "true"; claim missing and trust off;
           trust on with the domain allowed; trust on with the domain not allowed; policy disabled.
         - test_org_session.py: default False; True kept; get_session expiry matches get.
         - test_routes_org_identity.py: a verified and an unverified sign-in leave the matching flag.
         - test_single_user_auth.py: the flag is True.
      6. Run `ruff check .` and `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/test_org_identity.py tests/unit/web/test_org_session.py tests/unit/web/test_routes_org_identity.py tests/unit/web/test_single_user_auth.py -q passes
      - grep -n "def session_email_verified" src/privacyfence/org_identity.py finds it
      - grep -n "def get_session" src/privacyfence/web/org_session.py finds it

  - id: p3-authorize-token-userinfo
    title: The authorization-code flow with mandatory PKCE, ID tokens and userinfo
    depends_on: [p1-clients-and-keys, p2-session-email-verified]
    complexity: M
    worker_model: opus
    worker_model_reason: A sign-in endpoint for other apps; client authentication, PKCE, single-use codes, redirect-URI checks and error redirects must be exactly right, and a mistake moves a trust boundary.
    touches:
      - src/privacyfence/oidc_provider.py
      - src/privacyfence/web/routes_oidc.py
      - tests/unit/test_oidc_provider.py
      - tests/unit/web/test_routes_oidc.py
      - tests/unit/web/test_server_org_mode.py
      - CHANGELOG.md
    brief: |
      Read docs/oidc-provider-plan.md: "Provider state", "Endpoints" (authorize, token, userinfo) and
      "Risks".
      1. oidc_provider.py: CodeGrant and the remaining OidcProvider methods with exactly the
         signatures, pruning, caps, byte-safe secret comparison, PKCE check, ID token claims
         (including at_hash) and lifetimes in the Design.
      2. routes_oidc.py: the authorize, token and userinfo routes, with exactly the checks, order,
         status codes, JSON bodies, headers, messages, error-redirect construction and log line in the
         Design.
      3. Tests, with these names:
         - tests/unit/web/test_routes_oidc.py, using Starlette's TestClient and a real OrgSessionStore
           whose session the test creates:
           test_authorize_unknown_client_400, test_authorize_unregistered_redirect_400,
           test_authorize_without_session_redirects_to_login_keeping_query,
           test_authorize_bad_scope_redirects_error, test_authorize_missing_pkce_refused,
           test_authorize_plain_pkce_refused, test_error_redirect_merges_existing_query_and_omits_absent_state,
           test_full_flow_basic_auth (verifies the ID token with PyJWT against /oidc/jwks: iss, aud,
           nonce, at_hash, email, email_verified, exp-iat == 300), test_full_flow_post_auth,
           test_basic_and_form_secret_together_400, test_basic_auth_plus_encoded_secret,
           test_non_ascii_secret_is_401_not_500, test_code_single_use, test_code_expired,
           test_code_wrong_client, test_code_wrong_redirect, test_pkce_wrong_verifier,
           test_pkce_missing_verifier, test_token_bad_secret_401, test_userinfo_ok,
           test_userinfo_expired_401, test_unverified_session_gives_email_verified_false,
           test_scope_echoed, test_code_cap_temporarily_unavailable.
         - tests/unit/web/test_server_org_mode.py, through the full org app with a real OrgAuth and
           the oidc field set: test_oidc_token_with_issuer_host_and_no_origin_reaches_route,
           test_oidc_access_token_refused_at_mcp, test_mcp_token_refused_at_oidc_userinfo.
         - tests/unit/test_oidc_provider.py: pruning on insert, the access-token cap dropping the
           oldest, and expiry with an injected clock.
      4. CHANGELOG.md, "## [Unreleased]", "### Added":
         "- **Sign in to other apps with PrivacyFence.** `PRIVACYFENCE_OIDC_CLIENTS` names web apps, such as Peek and Grist, that sign people in through PrivacyFence (OpenID Connect at `<server>/oidc`), with the same identity PrivacyFence has."
      5. Run the whole gate:
         `pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json`,
         `python3 scripts/check_coverage_floor.py coverage.json`, `ruff check .`,
         `bandit -c pyproject.toml -r src`, `python3 -m mypy src/privacyfence` and
         `python3 scripts/mypy_strict_modules.py`. Stop conditions: see Risks.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_oidc.py tests/unit/test_oidc_provider.py tests/unit/web/test_server_org_mode.py -q passes, including every named test
      - python3 scripts/check_coverage_floor.py coverage.json passes
      - grep -n "OIDC sign-in to" src/privacyfence/web/routes_oidc.py finds the log line

  - id: p4-retire
    title: Reference docs, ADRs, delete the plan
    depends_on: [p3-authorize-token-userinfo]
    complexity: S
    touches:
      - docs/oidc-provider.md
      - docs/configuration-reference.md
      - docs/org-mode-docker.md
      - docs/peek-setup.md
      - docs/grist-setup.md
      - docs/README.md
      - docs/adr/0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md
      - docs/adr/0274-the-oidc-signing-key-is-generated-in-the-org-directory.md
      - docs/adr/README.md
      - docs/oidc-provider-plan.md
    brief: |
      1. New docs/oidc-provider.md, in the voice of docs/peek-setup.md. It covers:
         - what it is, and the variables with an example;
         - the endpoints and the issuer (<server>/oidc);
         - the redirect addresses for Peek (/oauth/oidc/callback) and Grist (/oauth2/callback);
         - that Grist needs GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT=true, and that Peek needs
           PEEK_OIDC_ALLOW_PRIVATE_ISSUER=true when it reaches PrivacyFence at a private address or
           at http://localhost;
         - email_verified, and key rotation;
         - single-user: one network namespace, and the cookie note;
         - troubleshooting: each error message from the Design.

         Link it from docs/README.md under "## User and operator docs", in the "Organization
         deployment and security" list.
      2. docs/configuration-reference.md and docs/org-mode-docker.md ("Environment variables"): add
         the variables. docs/peek-setup.md and docs/grist-setup.md: a short paragraph
         "Sign in to Peek (Grist) with PrivacyFence" linking to docs/oidc-provider.md.
      3. Write the two ADRs from the Design and "Rejected" sections, in the template in
         docs/adr/README.md, Status Accepted, with the next free numbers (0273 and 0274 unless taken),
         and add them to the index. Link source files and the PR, never this plan.
      4. Delete docs/oidc-provider-plan.md; `grep -rn "oidc-provider-plan" .` finds nothing.
      5. Run `python3 -m pytest tests/unit/test_website_docs_allowlist.py tests/unit/test_docs_links.py tests/unit/test_docs_no_history.py -q`.
    acceptance:
      - test ! -e docs/oidc-provider-plan.md
      - grep -rn "oidc-provider-plan" docs src finds nothing
      - grep -n "PRIVACYFENCE_OIDC_CLIENTS" docs/oidc-provider.md docs/configuration-reference.md docs/org-mode-docker.md finds each file
      - python3 -m pytest tests/unit/test_website_docs_allowlist.py tests/unit/test_docs_links.py tests/unit/test_docs_no_history.py -q passes
```
