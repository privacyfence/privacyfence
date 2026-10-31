# PrivacyFence as the sign-in provider for Peek and Grist

## Goal

An organization server, or a single-user install on `localhost`, can be the OpenID Connect sign-in
provider for a few web apps the operator names, such as Peek and Grist in the `privacyfence-suite`
Docker stack. People sign in to Peek and Grist with **Sign in with PrivacyFence**. They then use the
same identity PrivacyFence has from its own sign-in: the organization's identity provider, the
built-in Google sign-in, or the passkey on `localhost`. No app is ever registered with the identity
provider or with Google for Peek or Grist. The operator names each app, its redirect address and its
secret in PrivacyFence's environment, so the suite can generate them.

## Current state

- **PrivacyFence is an OAuth authorization server only for MCP clients** (ADR 0011):
  - The code is `OrgOAuthProvider` in `src/privacyfence/web/oauth_provider.py`, and the routes are
    mounted by `mount_org_oauth` in `src/privacyfence/web/routes_mcp.py:1052-1163`.
  - It serves `/.well-known/oauth-authorization-server`, `/authorize`, `/token`, `/register` and
    `/revoke`.
  - Tokens are opaque; there is no ID token, no JWKS, no userinfo and no
    `/.well-known/openid-configuration` anywhere in `src/`.
  - `org_identity.py` is the *client* side: it verifies the upstream IdP's ID tokens with PyJWT
    (`import jwt`, `PyJWKClient`, `org_identity.py:35-37`).
- **Browser sessions** (`src/privacyfence/web/org_session.py`):
  - `OrgSession(principal, created_at, last_seen_at)` (`:50`), with
    `OrgSessionStore.create(principal) -> str` (`:75`) and `get(session_id) -> Principal | None`
    (`:82`). There is no `email_verified` on the session or the `Principal`.
  - The IdP sign-in creates the session in `routes_org_identity.py:273`, where `claims` is in scope:
    `peek_provisioning.email_verified_claim(claims)` reads it on the next line.
  - Single-user creates it in `single_user_auth.py:301` for the fixed
    `SINGLE_USER_PRINCIPAL` (`admin@localhost`).
- **Sign-in redirect.** `/login?next=<local path>` signs a browser in, in both IdP mode
  (`routes_org_identity.py:318`) and single-user mode (`single_user_auth.py:361`), and returns to
  `next` (`safe_next_path`).
- **App wiring.** `OrgAuth` (`src/privacyfence/web/server.py:699-730`) carries the org-mode pieces to
  `_build_org_app` (`server.py:1326`), which mounts `mount_org_oauth` at `server.py:1374`.
  `daemon_main.py:1484` builds the `OrgAuth`.
- **Libraries.** `PyJWT` and `cryptography>=50.0.0` are already dependencies (`pyproject.toml`).
- **Constraining ADRs:**
  - 0011: org mode runs its own OAuth AS.
  - 0013: no MCP tool may mint a sign-in credential.
  - 0061: MCP tokens and browser sessions are audience-separated.
  - 0177: an MCP client needs consent once.
  - 0224: a registration is attested by its redirect URIs.
  - 0256/0264: startup settings come from the environment.

## Design

### Clients from the environment (`src/privacyfence/oidc_provider.py`, new)

```
PRIVACYFENCE_OIDC_CLIENTS=peek,grist
PRIVACYFENCE_OIDC_CLIENT_PEEK_REDIRECT_URIS=https://peek.example.com/oauth/oidc/callback
PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET_FILE=/run/suite/peek_oidc_client_secret.pf
PRIVACYFENCE_OIDC_CLIENT_GRIST_REDIRECT_URIS=https://grist.example.com/oauth2/callback
PRIVACYFENCE_OIDC_CLIENT_GRIST_SECRET_FILE=/run/suite/grist_oidc_client_secret.pf
```

- **Names.** Each name in `PRIVACYFENCE_OIDC_CLIENTS` (comma-separated, trimmed, lower-cased) must
  match `[a-z][a-z0-9-]{0,31}` and is the `client_id`. Its variable stem is
  `PRIVACYFENCE_OIDC_CLIENT_<NAME upper-cased, "-" → "_">_`.
- **Redirect URIs.** `…_REDIRECT_URIS` is comma-separated and needs at least one entry. Each must be
  `https://`, or `http://` with host `localhost`, `127.0.0.1` or `::1`. Each must have no userinfo and
  no fragment. They are compared exactly.
- **Secret.** `…_SECRET_FILE` or `…_SECRET` (never both; same rule as `org_mode.provisioning_key`,
  `org_mode.py:175-193`). It must hold at least 32 characters after `strip()`.
- **Exact `ConfigurationError` messages:**
  - `PRIVACYFENCE_OIDC_CLIENTS has an invalid name: <name>`
  - `<STEM>REDIRECT_URIS must list at least one address`
  - `<STEM>REDIRECT_URIS has an address that is not https:// (http:// only for localhost) or has a fragment: <uri>`
  - `Set <STEM>SECRET_FILE or <STEM>SECRET, not both.`
  - `<STEM>SECRET_FILE cannot be read: <OSError>`
  - `<STEM>SECRET must be at least 32 characters`
  - `PRIVACYFENCE_OIDC_CLIENTS is set, but this is not an organization server.` (local mode)
- **Data and parsing.**
  `@dataclass(frozen=True) class OidcClient: client_id: str; redirect_uris: tuple[str, ...]; secret: str = field(repr=False)`.
  `def clients_from_env() -> dict[str, OidcClient]` returns `{}` when the variable is unset or empty.

### Signing key

- **Generated once.** RSA 2048, made on first start with `cryptography` and written atomically to
  `paths.org_dir() / "oidc_signing_key.pem"` (PKCS8 PEM, mode `0600`), then reused.
- **Key id.** `kid` is the first 16 characters of the base64url (unpadded) SHA-256 of the DER
  SubjectPublicKeyInfo.
- **Algorithm.** RS256, because `openid-client` (Grist) defaults to it and `go-oidc` (Peek) accepts
  it.
- **Functions.** `load_or_create_signing_key() -> SigningKey` (a small dataclass holding the private
  key and `kid`) and `jwks(key) -> dict` (`{"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid", "n", "e"}]}`).
- **Rotation.** Deleting the file rotates the key at the next start. Already-issued ID tokens live
  only 5 minutes.

### Endpoints (`src/privacyfence/web/routes_oidc.py`, new)

The issuer is `<issuer_url>/oidc`, where `<issuer_url>` is `PRIVACYFENCE_ISSUER_URL` (or
`http://localhost:<port>` in single-user mode). Every endpoint lives under it, so it never mixes with
the MCP authorization server's metadata.

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

There is no `end_session_endpoint`; the apps sign out locally (Grist needs
`GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT=true`).

**`GET /oidc/jwks`** (no auth) returns `jwks(key)`.

**`GET /oidc/authorize`.** It checks, in this order:

1. **Client and redirect address.** `client_id` must be a configured client, else plain-text 400
   `Unknown client.`. `redirect_uri` must be one of its URIs, else plain-text 400
   `This redirect address is not registered for this client.`. These two never redirect.
2. **Request parameters.** After that, errors redirect to
   `redirect_uri?error=<code>&error_description=<text>&state=<state>`:
   - `response_type` other than `code` gives `unsupported_response_type`.
   - `scope` not containing `openid` gives `invalid_scope`.
   - `code_challenge_method` other than `S256` when `code_challenge` is present gives
     `invalid_request`, as does a missing `state`.
3. **Browser session.** With no valid browser session, it answers 302 to
   `/login?next=<urlencoded path and query of this request>`.
4. **Email.** A principal with no email gives `access_denied`.
5. **Code.** Otherwise it issues a code (`secrets.token_urlsafe(32)`, single use, 60 seconds) that
   remembers client, redirect_uri, principal, `email_verified`, `nonce`, `code_challenge` and
   `auth_time`. It answers 302 to `redirect_uri?code=<code>&state=<state>` with
   `Cache-Control: no-store`.

There is no consent page (ADR below): the operator configured these apps for their own people.

**`POST /oidc/token`** (form body; JSON answers with `Cache-Control: no-store`):

- **Client authentication.** By `Authorization: Basic` (URL-decoded, RFC 6749 §2.3.1) or by
  `client_id`/`client_secret` form fields, compared with `hmac.compare_digest`. A failure gives 401
  `{"error": "invalid_client"}` with `WWW-Authenticate: Basic`.
- **Grant type.** `grant_type` must be `authorization_code`, else 400
  `{"error": "unsupported_grant_type"}`.
- **The code.** The code is popped (single use). It must exist, be unexpired, belong to this client,
  and have the same `redirect_uri`. When it carried a challenge,
  `base64url(sha256(code_verifier)) == code_challenge` must hold. Anything else gives 400
  `{"error": "invalid_grant"}`.
- **Success.** It answers
  `{"access_token": <token_urlsafe(32)>, "token_type": "Bearer", "expires_in": 300, "scope": "openid email profile", "id_token": <jwt>}`.
  The ID token holds `iss`, `sub` (the principal id), `aud` (the client_id), `iat`, `exp` (`iat`+300),
  `auth_time`, `nonce` (when given), `email`, `email_verified` and `name` (the display name). It is
  signed RS256 with header `kid`.

**`GET` or `POST /oidc/userinfo`.** It needs `Authorization: Bearer <access_token>` from `/oidc/token`,
valid 300 seconds. It answers `{"sub", "email", "email_verified", "name"}`. A missing or unknown token
gives 401 `{"error": "invalid_token"}` with `WWW-Authenticate: Bearer error="invalid_token"`.

**State and logging.**

- Codes and access tokens live in memory, in an `OidcProvider` instance (not module-level), and an
  expired entry is dropped on access. A restart signs nobody out of Peek or Grist, which keep their
  own sessions.
- Each successful token exchange logs at INFO:
  `OIDC sign-in to <client_id> for principal <principal id>`. It never logs an email, code or token.

### `email_verified`

- `OrgSession` gains `email_verified: bool = False`, and
  `OrgSessionStore.create(principal, *, email_verified: bool = False)`.
- A new `OrgSessionStore.get_session(session_id) -> OrgSession | None` has the same expiry and
  last-seen behaviour as `get`, which is rewritten to return `get_session(...).principal`.
- **IdP sign-in** (`routes_org_identity.py:273`) passes `email_verified` as true when the claims say
  so, or when the IdP config has `trust_unverified_email`, the same rule
  `peek_provisioning.email_verified_claim` and the admin check use. The phase reuses that function
  rather than adding one.
- **Single-user sign-in** (`single_user_auth.py:301`) passes `email_verified=True`: the address is
  fixed in code and the install has one person (ADR 0272's reasoning applies).
- Peek and Grist refuse a sign-in whose `email_verified` is false. That is intended: the same rule
  guards automatic accounts (ADRs 0243, 0247).

### Wiring

- `OrgAuth` gains `oidc: OidcProvider | None = None`.
- `daemon_main.py` builds it at `OrgAuth(...)` (`daemon_main.py:1484`) when `clients_from_env()` is
  non-empty, in org mode (IdP or single-user), with `issuer=<issuer_url>/oidc`, the clients and
  `load_or_create_signing_key()`.
- `_build_org_app` mounts `routes_oidc.build_routes(org.oidc, sessions=org.sessions)` next to
  `mount_org_oauth` when `org.oidc` is set.
- At start it logs `OIDC provider for <n> app(s) at <issuer>`.

### Rejected

- **Reusing the MCP authorization server's `/authorize` and `/token`.** They are built by the MCP SDK
  for DCR clients with consent and opaque tokens, and audience-separated from browser sessions (ADR
  0061). An OIDC flow for first-party web apps has different clients, no consent and an ID token.
  Mixing them would weaken both.
- **Dynamic client registration for OIDC clients.** Any page could register itself and sign people in
  silently; only the operator names apps.
- **A consent page.** The operator named the app; a page asking every person "allow Peek?" adds a
  click and protects nothing.
- **Refresh tokens and long-lived access tokens.** Peek and Grist keep their own sessions and only
  need the ID token or userinfo at sign-in.
- **ES256 or EdDSA.** RS256 is the one algorithm every OIDC client library accepts by default.
- **Relaying the upstream IdP's ID token.** It names PrivacyFence's client as audience and does not
  exist in single-user or built-in-Google mode.

## ADRs

- **0273**: PrivacyFence is an OpenID Connect provider for web apps the operator names in the
  environment, with no consent page, no dynamic registration and no refresh tokens. It lives under
  `<issuer>/oidc`, apart from the MCP authorization server.
- **0274**: The OIDC signing key is an RS256 key generated in the org directory; deleting it rotates
  it.

(Numbers assume `plan/env-automatic-accounts` takes 0271 and 0272. If either plan lands first with
other numbers, the retirement phase takes the next free numbers instead.)

## Manual steps

None. The suite plan's `manual_after` signs in to Peek and Grist through PrivacyFence for real.

## Risks and open questions

- **A global middleware may refuse a POST without an `Origin` header** (server-to-server token
  calls). If `/oidc/token` answers 403 in a TestClient test without `Origin`, exempt `/oidc/token`
  and `/oidc/userinfo` the way the existing client-authenticated paths are handled
  (`_CLIENT_AUTHENTICATED_PATHS`, `routes_mcp.py:959`). If no such mechanism fits, stop with
  `status=blocked`.
- **The Host allowlist** (`server.py:1682-1692`) accepts only the issuer's host. Peek and Grist call
  `/oidc/token` through the proxy with that host, or through `localhost` in the suite's localhost
  stack, so nothing changes. If a test shows otherwise, stop with `status=blocked`.
- **`/login?next=`** must accept a next path with a query string (`/oidc/authorize?client_id=…`). If
  `safe_next_path` drops the query, stop with `status=blocked` and report its rules, rather than
  loosening them.

## Implementation manifest

```yaml
plan_slug: oidc-provider
feature_branch: feature/oidc-provider
max_parallel: 2
verify_after_merge:
  - python3 -m pytest tests/unit/test_oidc_provider.py tests/unit/web/test_routes_oidc.py tests/unit/web/test_org_session.py tests/unit/web/test_routes_org_identity.py tests/unit/web/test_single_user_auth.py -q
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
      - tests/unit/test_oidc_provider.py
      - tests/unit/web/test_routes_oidc.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read docs/oidc-provider-plan.md: "Clients from the environment", "Signing key", the
      discovery and JWKS parts of "Endpoints", and "Wiring".
      1. New src/privacyfence/oidc_provider.py: OidcClient, clients_from_env() with every message,
         SigningKey, load_or_create_signing_key() (atomic write: write to a temp file in the same
         directory with mode 0600, then os.replace), jwks(key), and a class OidcProvider with
         __init__(self, *, issuer: str, clients: dict[str, OidcClient], key: SigningKey,
         clock: Callable[[], float] = time.time). In this phase it holds only those and a
         discovery() -> dict method returning the Design's JSON. Follow the module and docstring
         conventions in docs/coding-and-testing-guidelines.md §1.2.
      2. New src/privacyfence/web/routes_oidc.py: build_routes(provider, *, sessions) -> list[Route]
         with the discovery and JWKS routes only.
      3. server.py: add the oidc field to OrgAuth (with a short comment like its neighbours) and
         mount the routes next to mount_org_oauth at server.py:1374 when org.oidc is set.
         daemon_main.py: build the OidcProvider at daemon_main.py:1484 as designed, and log the start
         line. In local mode a set PRIVACYFENCE_OIDC_CLIENTS raises the local-mode ConfigurationError.
      4. Tests: tests/unit/test_oidc_provider.py (every message; two clients parsed; secret not in
         repr; the key is created once with mode 0600 and reused; the kid is stable; the JWKS n/e
         match the key) and tests/unit/web/test_routes_oidc.py (discovery JSON exactly; the JWKS
         verifies a token signed with the key using PyJWT). tests/unit/test_daemon_main.py: the
         provider is built when the variable is set and absent otherwise. Use tmp_path for the org
         directory as the existing tests do.
      5. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`,
         `python3 scripts/mypy_strict_modules.py` and `bandit -c pyproject.toml -r src`.
    acceptance:
      - python3 -m pytest tests/unit/test_oidc_provider.py tests/unit/web/test_routes_oidc.py tests/unit/test_daemon_main.py -q passes
      - grep -n "def clients_from_env" src/privacyfence/oidc_provider.py finds it
      - grep -n "oidc_signing_key.pem" src/privacyfence/oidc_provider.py finds it
      - ruff check . and bandit -c pyproject.toml -r src pass

  - id: p2-session-email-verified
    title: Browser sessions remember whether the person's email is verified
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/web/org_session.py
      - src/privacyfence/web/routes_org_identity.py
      - src/privacyfence/web/single_user_auth.py
      - tests/unit/web/test_org_session.py
      - tests/unit/web/test_routes_org_identity.py
      - tests/unit/web/test_single_user_auth.py
    brief: |
      Read docs/oidc-provider-plan.md, section "email_verified".
      1. org_session.py: add email_verified to OrgSession with default False, the keyword argument to
         create(), and get_session(); rewrite get() on top of get_session() without changing its
         behaviour.
      2. routes_org_identity.py:273: pass
         email_verified=peek_provisioning.email_verified_claim(claims) to sessions.create. Check that
         email_verified_claim honours trust_unverified_email. If it does not, stop with
         status=blocked and report what it does; do not write a second rule.
      3. single_user_auth.py:301: pass email_verified=True, with a one-line comment (fixed address,
         one person).
      4. Tests: test_org_session.py (default False; True kept; get_session expiry matches get);
         test_routes_org_identity.py (a sign-in with email_verified true and one with false leave
         the matching session flag); test_single_user_auth.py (the session flag is True).
      5. Run `ruff check .` and `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/web/test_org_session.py tests/unit/web/test_routes_org_identity.py tests/unit/web/test_single_user_auth.py -q passes
      - grep -n "def get_session" src/privacyfence/web/org_session.py finds it

  - id: p3-authorize-token-userinfo
    title: The authorization-code flow with ID tokens and userinfo
    depends_on: [p1-clients-and-keys, p2-session-email-verified]
    complexity: M
    worker_model: opus
    worker_model_reason: A sign-in endpoint for other apps; client authentication, PKCE, single-use codes and redirect-URI checks must be exactly right, and a mistake moves a trust boundary.
    touches:
      - src/privacyfence/oidc_provider.py
      - src/privacyfence/web/routes_oidc.py
      - tests/unit/test_oidc_provider.py
      - tests/unit/web/test_routes_oidc.py
      - CHANGELOG.md
    brief: |
      Read docs/oidc-provider-plan.md: "Endpoints" (authorize, token, userinfo, state and logging) and
      "Risks".
      1. oidc_provider.py, OidcProvider: issue_code(...), redeem_code(...), issue_access_token(...),
         userinfo(token), id_token(...), with a threading.Lock around the two dicts. The clock is
         injected for tests. Client authentication is a method authenticate(client_id, secret) -> OidcClient | None
         using hmac.compare_digest.
      2. routes_oidc.py: the authorize, token and userinfo routes, with exactly the checks, order,
         status codes, JSON bodies, headers and messages in the Design. The session comes from
         sessions.get_session(<cookie org_session.SESSION_COOKIE>). The login redirect is
         /login?next=<urllib.parse.quote(path + "?" + query, safe="")>.
      3. Stop conditions: see Risks (Origin middleware; Host allowlist; next path with a query). Each
         one is a test that must pass, or a stop with status=blocked.
      4. Tests in tests/unit/web/test_routes_oidc.py with Starlette's TestClient and a fake session
         store. Required test names:
         test_authorize_unknown_client_400, test_authorize_unregistered_redirect_400,
         test_authorize_without_session_redirects_to_login, test_authorize_bad_scope_redirects_error,
         test_authorize_plain_pkce_refused, test_full_flow_basic_auth_pkce (verifies the ID token with
         PyJWT against /oidc/jwks: iss, aud, nonce, email, email_verified, exp-iat == 300),
         test_full_flow_post_auth_without_pkce, test_code_single_use, test_code_expired,
         test_code_wrong_client, test_code_wrong_redirect, test_pkce_wrong_verifier,
         test_token_bad_secret_401, test_userinfo_ok, test_userinfo_expired_401,
         test_token_without_origin_header_ok, test_unverified_session_gives_email_verified_false.
         Unit tests for OidcProvider in tests/unit/test_oidc_provider.py cover expiry and the lock path.
      5. CHANGELOG.md, "## [Unreleased]", "### Added":
         "- **Sign in to other apps with PrivacyFence.** `PRIVACYFENCE_OIDC_CLIENTS` names web apps, such as Peek and Grist, that sign people in through PrivacyFence (OpenID Connect at `<server>/oidc`), with the same identity PrivacyFence has."
      6. Run the whole gate: `pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json`,
         `python3 scripts/check_coverage_floor.py coverage.json`, `ruff check .`,
         `bandit -c pyproject.toml -r src`, `python3 -m mypy src/privacyfence` and
         `python3 scripts/mypy_strict_modules.py`.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_oidc.py tests/unit/test_oidc_provider.py -q passes, including every named test
      - python3 scripts/check_coverage_floor.py coverage.json passes
      - grep -n "OIDC sign-in to" src/privacyfence/web/routes_oidc.py src/privacyfence/oidc_provider.py finds the log line

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
      1. New docs/oidc-provider.md, in the voice of docs/peek-setup.md:
         - What it is, and the variables with an example.
         - The endpoints and the issuer (<server>/oidc).
         - The redirect addresses for Peek (`/oauth/oidc/callback`) and Grist (`/oauth2/callback`).
         - That Grist needs GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT=true, and that Peek needs
           PEEK_OIDC_ALLOW_PRIVATE_ISSUER=true when it reaches PrivacyFence at a private address.
         - email_verified, key rotation, and troubleshooting (each error message from the Design).
         Link it from docs/README.md.
      2. docs/configuration-reference.md and docs/org-mode-docker.md (Environment variables): add the
         variables. docs/peek-setup.md and docs/grist-setup.md: a short paragraph "Sign in to Peek
         (Grist) with PrivacyFence" linking to docs/oidc-provider.md.
      3. Write the two ADRs from the Design and "Rejected" sections, in the template in
         docs/adr/README.md, Status Accepted, with the next free numbers (0273 and 0274 unless taken),
         and add them to the index. Link source files and the PR, never this plan.
      4. Delete docs/oidc-provider-plan.md; `grep -rn "oidc-provider-plan" .` finds nothing.
    acceptance:
      - test ! -e docs/oidc-provider-plan.md
      - grep -rn "oidc-provider-plan" docs src finds nothing
      - grep -n "PRIVACYFENCE_OIDC_CLIENTS" docs/oidc-provider.md docs/configuration-reference.md docs/org-mode-docker.md finds each file
```
