# Grist connector plan

## 1. Goal

Add a **Grist** connector, so an AI client can list a user's Grist tables and columns, read
records after review, and (with approval on a card) add and update records and add tables and
columns. People sign in with **Grist's OAuth**, the same way they sign in to Salesforce or
Atlassian. An administrator registers a PrivacyFence app in Grist (**Account settings →
Developer → OAuth apps**) and puts the server address, client id and secret in the organization
bundle. Each person then clicks **Authenticate…** in local mode, or **Connect** on `/connect` in
org mode. The app requests `doc:read`, `doc:write`, `doc.schema:write` and `offline_access`, and
nothing more. Nothing the connector does deletes: no record, column or table delete, no rename
and no type change.

Scope was confirmed with the maintainer while planning: OAuth only (no pasted API key); keep the
schema tools, knowing `doc.schema:write` is powerful; records-only reads (no SQL tool); local and
org mode.

## 2. Current state

- **Connectors** are `Connector` subclasses (`src/privacyfence/connector.py:78-96`;
  `ToolParam`/`ToolSpec` at `:16-75`), discovered automatically by
  `connector_catalog.connector_classes()` (`src/privacyfence/connector_catalog.py:20-29`). The
  closest template for a connector's shape is `connectors/salesforce.py` (653 lines, four tools,
  all reads): `_fetch` (`:628-632`) wraps the client in `asyncio.to_thread` and re-raises
  `SalesforceClientError` as `RuntimeError`; `_auto_audit` (`:634-653`) audits ungated tools;
  `_get_record` (`:403-447`) is the review-gated read pattern, with `preview`, `new_info`,
  `details_text`, `preview_tables`, `table_only=True` and `args`. Salesforce has **no write
  tools**. The popup write pattern is `connectors/contacts.py:305-342` (`contacts_update`,
  `old → new` previews) and `connectors/jira.py:806-839` (`jira_create_issue`).
- **OAuth connectors** are the template for authentication. Salesforce:
  `salesforce_client.py` `build_authorize_url` (`:420-438`), `exchange_code` (`:441-479`),
  `save_token_file`/`authorize_interactive` (`:482-514`, through
  `oauth_loopback.run_browser_oauth` with a fixed port `SALESFORCE_OAUTH_PORT = 53683` and
  `redirect_host="localhost"`), `load_token_file` (`:517-525`), refresh in `_try_refresh`
  (`:600-640`). Slack uses port 53682 and Atlassian 53684 (`atlassian_oauth.py:25`).
  `oauth_loopback.run_browser_oauth` (`:148`) does the loopback listener, `state` and PKCE.
- **Daemon wiring**: `daemon_main.TOKEN_FILES` (`:169-180`), resolved per principal by
  `_resolve_path` (`:280-299`). `build_connectors()` (`:1275-1578`) builds each connector in a
  `try`; the Salesforce block (`:1466-1481`) is the pattern; `_classify_connector_failure`
  (`:1222-1258`) maps a message containing `"Use Authenticate…"` to `not_authenticated` and one
  containing `"organization config not installed"` to `no_org_config`. CLI:
  `run_salesforce_oauth` (`:1786-1802`) and the `--salesforce-oauth` flag (`:2190`, dispatched at
  `:2314-2335`). Token writers go through `secure_files.atomic_write_json` (mode `0600`) and are
  listed in `TOKEN_WRITE_SITES` (`tests/unit/test_systemic_gate_invariants.py:177-188`, 10
  entries, pinned by `test_ten_token_write_sites_are_listed` at `:248`).
- **Settings page (local mode)**: `settings_controller.authenticate_connector` (`:1258-1275`)
  dispatches to `_authenticate_salesforce` (`:1331-1357`) and friends; `ALL_CONNECTORS`
  (`:89-92`), `ORG_CONFIG_SERVICE` (`:115-121`), `ORG_BUNDLE_SERVICES` (`:122`). The page's
  Authenticate… button is generic, so no HTML change is needed for an OAuth connector.
- **Org mode**: `web/routes_connect.py` `OAUTH_SERVICES` (`:100`), `_GRANT_KEY` (`:108-109`),
  `SERVICE_LABELS` (`:112-115`), `_ORG_CONFIG_SECTION` (`:118-119`), `_is_configured`
  (`:138-148`), `_build_authorize_url` (`:244-284`), `_exchange_and_save` (`:290-330`); the
  `/oauth/start/{service}` and `/oauth/callback/{service}` routes are generic. The bundle is built
  by `scripts/build_org_bundle.py` (standard library only): one argument group and section per
  service, `_CONNECTOR_CALLBACKS` (`:52-57`, indexed for every service at `:744`), the
  `services` tuple at `:692`.
- **QA**: `scripts/qa_authenticate_connectors.py` `STEPS` (`:74-84`) runs each `--<x>-oauth` flag;
  `scripts/qa_fixture_recorder.py` `CONNECTOR_CHECKS` (`:1776`) and `EXPECTED_FIXTURES` (`:1802`)
  must have equal keys (import-time assert); `RawCapture` (`:595-628`) wraps a client's
  `_request(fn, *args, **kwargs)`; `LIFECYCLE_CHECKS` (`:2353`); `lifecycle_confluence`
  (`:2195-2240`) is the create-and-update-without-delete precedent. Live credentials exist only on
  the self-hosted runner under `~/privacyfence/` (ADR 0019, `docs/connector-qa.md` "Persistent QA
  state").
- **Policy tables** a new tool must appear in: `auto_accept.TOOL_TO_GATE` (`:177`),
  `TOOL_TO_OPERATION` (`:84`), `policy/registry.py` `TOOL_TO_VERB` (`:141`),
  `write_effects.EFFECT_BY_TOOL` (`:42`), optionally `gate._TOOL_LAYOUT` (`:254-296`).
  `policy/scopes.SCOPE_SELECTORS` is the frozen set checked against `tests/unit/policy/_v1_reference.py`;
  a scope with no v1 predicate goes in `NEW_SCOPE_SELECTORS` (`:555`) and is offered through
  `policy/catalogue.EXTRA_SCOPES` (`:47`), the Apps Script precedent (`apps_script.project`).
  `policy/propose.py` only proposes from `SCOPE_SELECTORS` (`:514-516`), so an `EXTRA_SCOPES`
  operation never gets the popup's "Always allow" (ADR 0077).
- **Every-connector bookkeeping enforced by tests**: `tests/unit/connectors/test_readme_manifest_alignment.py`
  `CONNECTOR_CLASSES` (`:36-40`), `scripts/generate_tools_reference.py`
  `CONNECTOR_TITLES`/`CONNECTOR_SHORT` (`:44-70`), `docs/tools-reference.md` (generated),
  `docs/always-allow-rules-reference.md` (generated; lists every review and popup tool),
  `scripts/pyinstaller_common.py` hidden imports (`:74-86`), `tests/unit/test_website_connector_pages.py`
  `CONNECTORS` (`:28-40`: a README row, a setup guide in `scripts/build_site.py` `CONNECTOR_GUIDES`
  and a page in `PAGES`), `website/_partials/other-connectors.html` (one line per connector page;
  `build_site._without_current_connector` raises without it), `tests/unit/test_website_connectors_page.py`
  (card counts, `"120 tools"` / `"Eleven connectors"`, `"120 connector tools"` on how-it-works,
  `len(REFERENCE) == 11`), `tests/unit/test_website_docs_allowlist.py` (every `docs/*.md` is
  listed in `docs/README.md` or `CONTRIBUTOR_DOCS`), the plugin `RESERVED_PLUGIN_NAMES` in four
  synced copies, `tests/unit/test_connector_tool_annotations.py` (Grist adds no destructive tool),
  and `tests/unit/test_systemic_gate_invariants.py`.
- Next free ADR number: **0142**.

## 3. Design

### 3.0 Rules for every phase

- No project history in code, comments, docstrings, user-visible strings or standing docs: no phase
  ids (`p3`), plan names, "Phase N", bare `#123` issue numbers or "as of" phrasing
  (`tests/unit/test_code_no_history.py`, `tests/unit/test_docs_no_history.py`, ADR 0056). Cite an ADR
  number for a reason instead.
- Every `gated_call(...)` in `connectors/grist.py` passes its tool name as a string literal
  (`tool="grist_add_records"`) and its gate as a literal (`gate="popup"`), inline at the call site.
  `test_readme_manifest_alignment.py` (`_SRC_TOOL_GATE_RE`), `test_write_effects.py` and
  `test_systemic_gate_invariants.py` read the source; a shared helper that passes the tool name as a
  variable defeats them and is not allowed.
- This plan and its manual-steps page are listed in `scripts/build_site.py` `CONTRIBUTOR_DOCS` and in
  `docs/README.md`'s contributor half while the work is open (the plan branch's own commit does
  that, so `tests/unit/test_website_docs_allowlist.py` passes); the last phase removes both entries.
- Nothing in this connector reads, stores or asks for a Grist API key; sign-in is OAuth only (ADR 0142).
- Every phase ends with `ruff check .` and `python3 -m pytest tests/unit -q` passing in full.

### 3.1 Grist API and OAuth used

**OAuth** (Grist help, "OAuth apps"): confidential clients only (every app has a client secret;
`token_endpoint_auth_method=none` is not supported); PKCE is required (S256); `offline_access`
issues a refresh token and requires `prompt=consent`; access tokens (`grist_at_…`) last 1 hour,
refresh tokens (`grist_rt_…`) 60 days and are rotated when used late in their life (replace the
stored one whenever a refresh returns a new `refresh_token`); errors are HTTP 4xx with JSON
`error`/`error_description`; `localhost` redirect URIs may be `http://`; an app can register
several redirect URIs, each matched exactly. Endpoints come from the RFC 8414 discovery document
`GET {auth_server_url}/.well-known/oauth-authorization-server` (`authorization_endpoint`,
`token_endpoint`, `token_endpoint_auth_methods_supported`). Seen live on getgrist.com:
`https://docs.getgrist.com/.well-known/oauth-authorization-server` and the same path on
`login.getgrist.com` both return issuer `https://login.getgrist.com/`, `authorization_endpoint`
`https://login.getgrist.com/oidc/auth`, `token_endpoint` `https://login.getgrist.com/oidc/token`,
`code_challenge_methods_supported` `["S256"]`, and `token_endpoint_auth_methods_supported`
including `client_secret_basic` and `client_secret_post`. Discovery therefore starts at the Grist
server and follows the issuer it names (§3.2.1); the optional bundle value `auth_server_url` is
only for a server that does not serve the document itself. OAuth apps are part of Grist's full edition for self-hosted
servers.

**Scopes requested**: exactly `doc:read doc:write doc.schema:write offline_access`. Never
`doc:download`, `doc:webhooks` or `user.profile:read`.

**REST** (Grist OpenAPI, `gristlabs/grist-help` `api/grist.yml`). Base `{server_url}/api`,
header `Authorization: Bearer <access token>`:

| Client method | HTTP | OAuth scope in the API reference |
|---|---|---|
| `list_documents` | `GET /api/orgs`, then `GET /api/orgs/{orgId}/workspaces` per org | none listed (API key only) |
| `get_document` | `GET /api/docs/{docId}` (`name`, `workspace.name`, `workspace.org.name`) | none listed (API key only) |
| `list_tables` | `GET /api/docs/{docId}/tables`, then `…/tables/{tableId}/columns` per table | `doc:read` |
| `list_columns` | `GET /api/docs/{docId}/tables/{tableId}/columns` (`columns[].id`, `fields.label`, `fields.type`, `fields.isFormula`) | `doc:read` |
| `get_records` | `GET …/records?filter=<json>&sort=<csv>&limit=<n>` → `{"records":[{"id","fields"}]}` | `doc:read` |
| `add_records` | `POST …/records` body `{"records":[{"fields":{…}}]}` → `{"records":[{"id"}]}` | `doc:write` |
| `update_records` | `PATCH …/records` body `{"records":[{"id","fields":{…}}]}` | `doc:write` |
| `add_table` | `POST /api/docs/{docId}/tables` body `{"tables":[{"id","columns":[{"id","fields":{"label","type"}}]}]}` → `{"tables":[{"id"}]}` | `doc.schema:write` |
| `add_columns` | `POST …/tables/{tableId}/columns` body `{"columns":[{"id","fields":{"label","type"}}]}` → `{"columns":[{"id"}]}` | `doc.schema:write` |

The first two may refuse an OAuth token (HTTP 403); §3.3 handles that without depending on them.
The `records/delete` endpoint and every other delete, rename and column-modify endpoint are never
called (§3.7).

### 3.2 Grist modules

#### 3.2.1 `src/privacyfence/grist_oauth.py`

Module docstring: Grist OAuth (authorization code + PKCE, confidential client, refresh tokens);
the bundle's `grist` section is the app; the token is only ever used with the server it was
issued for; redirects are never followed; nothing logs a token or the client secret.

- `class GristClientError(Exception)` lives here (the REST client imports and re-exports it), and
  `class GristAccessDenied(GristClientError)` for HTTP 403.
- Constants: `GRIST_OAUTH_PORT = 53685`, `GRIST_REDIRECT_PATH = "/callback"` (local redirect
  `http://localhost:53685/callback`), `GRIST_SCOPES = "doc:read doc:write doc.schema:write offline_access"`,
  `DEFAULT_SERVER_URL = "https://docs.getgrist.com"`.
- `normalize_server_url(url: str) -> str`: strip whitespace and trailing `/`; `urllib.parse.urlsplit`.
  Every rejection raises `GristClientError` (never `ValueError`), so a bad bundle URL is caught by
  every `except GristClientError` in §3.5 and §3.6. Messages (exact): empty → `"Enter the Grist server address, such as https://docs.getgrist.com."`;
  a scheme other than `https`, except `http` with host `localhost`/`127.0.0.1`/`::1` →
  `"The Grist server address must start with https:// (http:// is allowed only for localhost)."`;
  userinfo, query or fragment → `"The Grist server address must not contain a user name, password, query or fragment."`;
  a path segment `api` → `"Enter the server address without /api."`. A path prefix is kept. Returns
  `scheme://netloc[/path]` with scheme and host lower-cased.
- Bundle section `grist` = `{"server_url": str, "client_id": str, "client_secret": str, "auth_server_url": str (optional)}`.
  `oauth_config(section: dict) -> GristOAuthConfig` (frozen dataclass `server_url`, `client_id`,
  `client_secret`, `auth_server_url`) normalizes both URLs (`auth_server_url` defaults to
  `server_url`); a missing `server_url`, `client_id` or `client_secret` →
  `GristClientError("Grist organization config not installed")`.
- `@dataclass(frozen=True) class GristOAuthEndpoints(authorization_endpoint: str, token_endpoint: str, client_secret_basic: bool)`.
- `discover(auth_server_url: str, server_url: str) -> GristOAuthEndpoints`: `GET {auth_server_url}/.well-known/oauth-authorization-server`
  (`timeout=30`, `allow_redirects=False`). Let `issuer` be the document's `issuer` with a trailing
  `/` stripped, normalized like a server URL. If `issuer != auth_server_url` (getgrist.com does this:
  `https://docs.getgrist.com/.well-known/oauth-authorization-server` names the issuer
  `https://login.getgrist.com/`), fetch `{issuer}/.well-known/oauth-authorization-server` once,
  require that document's own `issuer` to equal `issuer` (RFC 8414 §3.3), and use it; the configured
  server vouches for the issuer it names, and nothing is followed further. Both endpoints must be
  present, pass the same scheme rule as `normalize_server_url`, and have exactly the issuer's host
  (the token endpoint receives the client secret and the refresh token). Otherwise `GristClientError(f"Grist's sign-in settings at {host} are not usable. Check the Grist server address (and, for getgrist.com, the sign-in server) in the organization config.")`.
  `client_secret_basic` is `True` when `token_endpoint_auth_methods_supported` is absent (the
  RFC 8414 default) or contains `"client_secret_basic"`; otherwise the secret is sent in the form
  (`client_secret_post`). getgrist.com lists `client_secret_basic`. Results are cached per `auth_server_url` in a module dict (add its reset
  to `tests/conftest.py` `_reset()`).
- Client authentication at the token endpoint: with `client_secret_basic`, an `Authorization:
  Basic` header of `quote(client_id, safe="") + ":" + quote(client_secret, safe="")`
  (`urllib.parse.quote`, RFC 6749 §2.3.1), built by hand rather than with `requests`' `auth=`;
  otherwise `client_id` and `client_secret` in the form.
- Failures of the three sign-in requests (`discover`, `exchange_code`, `refresh`) all raise
  `GristClientError`, with these exact messages unless a bullet below gives a more specific one:
  `requests.RequestException` → `f"Could not reach Grist's sign-in server at {host}: {type(exc).__name__}"`;
  3xx → `f"Grist's sign-in server answered with a redirect (HTTP {status}). Check the sign-in server in the organization config."`;
  5xx → `f"Grist's sign-in server answered with an error (HTTP {status})."`;
  a 2xx body that is not a JSON object → `f"Grist's sign-in server answered with something other than JSON (HTTP {status})."`.
- `build_authorize_url(endpoints, client_id, redirect_uri, state, code_challenge) -> str`: params
  `response_type=code`, `client_id`, `redirect_uri`, `state`, `scope=GRIST_SCOPES`,
  `prompt=consent`, `code_challenge`, `code_challenge_method=S256`.
- `exchange_code(config, endpoints, code, redirect_uri, code_verifier) -> dict[str, Any]`: POST
  form `grant_type=authorization_code`, `code`, `redirect_uri`, `code_verifier` (client auth as
  `discover` decided). Returns the token record (below). No `refresh_token` in the answer →
  `GristClientError("Grist did not return a refresh token. Check that the PrivacyFence app in Grist allows offline_access.")`.
  A 4xx → `GristClientError(f"Grist sign-in failed: {error}: {error_description}")` (each cut to 200 characters).
- Token record and file `credentials/grist_token.json`:
  `{"server_url": <normalized>, "access_token": str, "refresh_token": str, "expires_at": float}`
  (`expires_at` = now + `expires_in`, default 3600). `save_token_file(path, record)` →
  `secure_files.atomic_write_json`. `load_token_file(path)`: missing file, or missing
  `server_url`/`refresh_token` → `GristClientError("Grist is not authenticated. Use Authenticate… in PrivacyFence Settings.")`;
  not valid JSON or not an object → `GristClientError("Grist's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to sign in again.")`.
  `check_server_matches(record, config)`: `normalize_server_url(record["server_url"]) != config.server_url` →
  `GristClientError("Grist was signed in to a different server than your organization uses. Use Authenticate… in PrivacyFence Settings to sign in again.")`.
- `refresh(config, endpoints, record) -> dict[str, Any]`: POST `grant_type=refresh_token`,
  `refresh_token`; returns `{**record, "access_token": …, "expires_at": …}` plus the new
  `refresh_token` when one comes back (so `server_url` and an unrotated `refresh_token` carry
  over). `invalid_grant` (or 400/401 with no JSON body) →
  `GristClientError("Your Grist sign-in has expired or was revoked. Use Authenticate… in PrivacyFence Settings to sign in again.")`;
  any other 4xx → `GristClientError(f"Grist sign-in refresh failed: {error}: {error_description}")` (each cut to 200 characters).
- `class GristTokenProvider(config: GristOAuthConfig, token_file: str)`: `access_token(*, force_refresh: bool = False) -> str`,
  thread-safe (`threading.Lock`); loads the record on first use, refreshes when `force_refresh` or
  `expires_at - 60 <= time.time()` (calling `discover(config.auth_server_url, config.server_url)`
  at the first refresh, then the cache), and saves the new record with `save_token_file`.
- `authorize_interactive(config, token_file) -> dict[str, Any]`: `discover(config.auth_server_url, config.server_url)`, then
  `oauth_loopback.run_browser_oauth(_build, _exchange, port=GRIST_OAUTH_PORT, path=GRIST_REDIRECT_PATH, redirect_host="localhost")`
  (the `salesforce_client.authorize_interactive` shape), maps `OAuthLoopbackError` to
  `GristClientError(f"Grist sign-in failed: {exc}")`, saves and returns the record.

#### 3.2.2 `src/privacyfence/grist_client.py`

Module docstring: the Grist REST client; tokens come from `GristTokenProvider`; never follows a
redirect; logs the host and counts only, never a token or a cell value.

- `from .grist_oauth import GristAccessDenied, GristClientError` (re-exported in `__all__`).
- HTTP: `requests` (already a dependency), one `requests.Session`, every call
  `session.request(method, url, params=..., json=..., headers=..., timeout=30, allow_redirects=False)`;
  TLS verification on (never `verify=False`).
- Dataclasses (all fields typed): `GristDocument(id, name, workspace, team)`,
  `GristColumn(id, label, type, is_formula: bool)`, `GristTable(id, columns: list[GristColumn] = field(default_factory=list))`,
  `GristRecord(id: int, fields: dict[str, Any] = field(default_factory=dict))`,
  `GristRecordPage(records: list[GristRecord], truncated: bool)`.
- Id validation (module-level, `.fullmatch()`): `DOC_ID_RE = re.compile(r"[A-Za-z0-9_~-]{1,128}")`,
  `IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")`. `validate_doc_id(v)` →
  `GristClientError(f"Not a Grist document id: {v!r}")`; `validate_identifier(v, kind)` (kind
  `"table"` or `"column"`) → `GristClientError(f"Not a Grist {kind} id: {v!r}")`. They exist
  because ids go into the URL path.
- `class GristClient(server_url: str, tokens: GristTokenProvider)`; `host` property (server host
  with port); `__repr__` without tokens.
  - `_request(self, method: str, path: str, *, params: dict[str, str] | None = None, json_body: Any = None) -> Any`:
    the single choke point (`RawCapture` wraps it; keep `method` first). Adds the bearer token; on
    401 calls `tokens.access_token(force_refresh=True)` and retries once. Returns parsed JSON or
    `None` for an empty body. Errors (exact):
    - `requests.RequestException` → `f"Could not reach the Grist server at {host}: {type(exc).__name__}"`
    - 3xx → `f"The Grist server answered with a redirect (HTTP {status}). Check the Grist server address in the organization config."`
    - 401 after the retry → `"Grist refused the sign-in (HTTP 401). Use Authenticate… in PrivacyFence Settings to sign in again."`
    - 403 → `GristAccessDenied("Grist refused the request (HTTP 403). Your Grist account, or what you allowed PrivacyFence when signing in, does not cover it.")`
    - 404 → `"Grist found no such document, table or record (HTTP 404)."`
    - other non-2xx → `f"Grist API error (HTTP {status}): {detail}"` (`detail` = JSON `error`, cut to 200 characters, else `"no detail"`)
    - a 2xx whose non-empty body is not JSON → `f"The Grist server answered with something other than JSON (HTTP {status}). Check the Grist server address in the organization config."`
  - `check_connection() -> str`: `tokens.access_token()` (a refresh when needed); returns `host`.
    It calls no REST endpoint, because the account-level endpoints may refuse OAuth tokens (§3.1).
  - `list_documents() -> list[GristDocument]` (at most 20 orgs and 500 documents, sorted by
    `(team, workspace, name)`; `id` is the document's `id`, never `urlId`; `team` is the org's
    `name`), `get_document(doc_id)`, `list_columns(doc_id, table_id)` (`label` falls back to `id`),
    `list_tables(doc_id)` (at most 100 tables), `get_records(doc_id, table_id, *, filters, sort, limit)`
    (requests `limit + 1`, sets `truncated`), `get_records_by_id(doc_id, table_id, ids)` (filter
    `{"id": ids}`), `add_records(...) -> list[int]`, `update_records(..., rows: list[tuple[int, dict]]) -> None`,
    `add_table(doc_id, table_id, columns) -> str`, `add_columns(doc_id, table_id, columns) -> list[str]`.
    Every method validates its ids first.

### 3.3 `src/privacyfence/connectors/grist.py`

`class GristConnector(Connector)`, `name == "grist"`, `__init__(self, client: GristClient)`.
`_auto_audit` copied from `connectors/salesforce.py:634-653`. `_fetch` is the Salesforce one
(`:628-632`) widened to keyword arguments: `async def _fetch(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any`
calling `await asyncio.to_thread(func, *args, **kwargs)` and re-raising `GristClientError` as
`RuntimeError(str(exc)) from exc` (after `logger.warning`).
Argument validation raises `ValueError` before anything is fetched or gated.

**Document details under OAuth.** Grist's API reference lists only API-key security for
`GET /api/orgs`, `GET /api/orgs/{orgId}/workspaces` and `GET /api/docs/{docId}`, while the table,
column and record endpoints list OAuth scopes. The connector therefore never depends on them:
- `_doc_info(doc_id) -> GristDocument` calls `client.get_document(doc_id)` and, when the client
  raises `GristAccessDenied` (§3.2.1), returns `GristDocument(id=doc_id, name=doc_id, workspace="", team="")`.
  Every gated tool uses `_doc_info` for the card; `"Team"` shows `doc.team or "(not shown by Grist)"`.
- `grist_list_documents` lets `GristAccessDenied` become
  `RuntimeError("This Grist server does not let PrivacyFence list your documents. Open the document in Grist and use the document id from its address (the part after the server address, before the next /).")`;
  any other `GristClientError` becomes `RuntimeError(str(exc))` as usual. Its description says so in
  one sentence ("Some Grist servers do not allow apps to list documents; then ask the user for the
  document id from its address."). JSON-string parameters
(`ToolParam` has no list type, as in Salesforce's `filters`) are parsed with `json.loads`; a parse
failure or wrong shape raises `ValueError` with the message given below.

Tools (every one ends with the required `ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")`;
`read_only` is `True` for the three reads and `False` for the four writes; none is `destructive`):

| Tool | Gate | Params (besides `reason`) | Returns |
|---|---|---|---|
| `grist_list_documents` | auto | none | `{"documents": [{"id","name","workspace","team"}]}` |
| `grist_list_tables` | auto | `doc_id` | `{"doc_id", "tables": [{"id", "columns": [{"id","label","type","is_formula"}]}]}` |
| `grist_get_records` | review | `doc_id`, `table_id`, `filter` (str, default `""`), `sort` (str, default `""`), `limit` (int, default 100) | `{"doc_id","table_id","records":[{"id","fields"}],"truncated": bool}` |
| `grist_add_records` | popup | `doc_id`, `table_id`, `records` (str) | `{"doc_id","table_id","added_ids":[…]}` |
| `grist_update_records` | popup | `doc_id`, `table_id`, `records` (str) | `{"doc_id","table_id","updated_ids":[…]}` |
| `grist_create_table` | popup | `doc_id`, `table_id`, `columns` (str) | `{"doc_id","table_id": <id Grist assigned>,"column_ids":[…]}` |
| `grist_add_columns` | popup | `doc_id`, `table_id`, `columns` (str) | `{"doc_id","table_id","column_ids":[…]}` |

Descriptions follow ADR 0115 (`assert_tool_definitions_complete`): a one-sentence summary first, a
`Returns` sentence, the related tool, and the approval wording (`"Auto-approved."`,
`"Requires user approval."`, as the Salesforce tools word them). First sentences (they become the
tools-reference rows):

- `grist_list_documents`: "List the Grist documents this account can open, with their workspace and team."
- `grist_list_tables`: "List the tables of one Grist document and each table's columns (id, label and type)."
- `grist_get_records`: "Read records from one table of a Grist document, optionally filtered and sorted."
- `grist_add_records`: "Add new records to one table of a Grist document."
- `grist_update_records`: "Change cells of existing records in one table of a Grist document."
- `grist_create_table`: "Create a new table, with its columns, in a Grist document."
- `grist_add_columns`: "Add new columns to an existing table of a Grist document."

Parameter descriptions: `doc_id` "The document id from grist_list_documents (the part of a Grist
URL after /doc/ or the docs.getgrist.com/ host, such as 8CAN8gKdxY7z)."; `table_id` "The table id
from grist_list_tables, such as Contacts (not the label shown in Grist)."; `filter` "A JSON object
mapping column ids to lists of allowed values, such as {\"Status\": [\"Open\", \"Waiting\"]}. Rows
match when every listed column has one of its values. Empty means no filter."; `sort` "Column ids
separated by commas, each optionally prefixed with - for descending, such as -Date,Name. Empty means
Grist's own order."; `limit` "Most records to return, 1 to 500. Default 100; the result says
truncated when there were more."; `records` (add) "A JSON array of objects, each mapping column ids
to the new record's values, such as [{\"Name\": \"Ada\", \"Score\": 3}]. 1 to 100 records; values
are text, numbers, true/false or null."; `records` (update) "A JSON array of {\"id\": <record id>,
\"fields\": {<column id>: <new value>}} objects, such as [{\"id\": 5, \"fields\": {\"Status\":
\"Done\"}}]. 1 to 100 records; only the listed columns change."; `columns` "A JSON array of {\"id\":
<column id>, \"label\": <optional label>, \"type\": <optional type>} objects. Types: Text (default),
Numeric, Int, Bool, Date, Choice, ChoiceList, Any, Ref:<TableId>, RefList:<TableId>. 1 to 50 columns."

Siblings map for `TestToolDefinitions`: `grist_list_documents`↔`grist_list_tables`,
`grist_list_tables`↔`grist_get_records`, `grist_add_records`↔`grist_update_records`,
`grist_create_table`↔`grist_add_columns`. Each description names its sibling.

Validation (shared helpers in the connector module; messages exact):

- `filter`: object with at most 20 keys, each key a valid column id, each value a non-empty list of
  scalars (`str`, `int`, `float`, `bool`, `None`) → otherwise `ValueError('filter must be a JSON object mapping column ids to lists of values, such as {"Status": ["Open"]}.')`.
- `sort`: comma-separated; each item an optional `-` then a valid column id → otherwise
  `ValueError("sort must be column ids separated by commas, each optionally prefixed with -.")`.
- `limit` outside 1–500 → `ValueError("limit must be between 1 and 500.")`.
- `records` (add): list of 1–100 objects, keys valid column ids, values scalars → otherwise
  `ValueError('records must be a JSON array of 1 to 100 objects mapping column ids to text, numbers, true/false or null.')`.
- `records` (update): list of 1–100 `{"id": int > 0, "fields": {non-empty object as above}}`, no
  duplicate id → otherwise `ValueError('records must be a JSON array of 1 to 100 {"id": <record id>, "fields": {...}} objects with distinct ids.')`.
- `columns`: list of 1–50 objects with a valid column `id`, optional `label` (str, ≤100 chars),
  optional `type` (default `"Text"`), no duplicate id → otherwise
  `ValueError('columns must be a JSON array of 1 to 50 {"id": ..., "label": ..., "type": ...} objects with distinct ids.')`.
  `type` must be one of `Text`, `Numeric`, `Int`, `Bool`, `Date`, `Choice`, `ChoiceList`, `Any`,
  or `Ref:<TableId>` / `RefList:<TableId>` with a valid table id → otherwise
  `ValueError(f"Unsupported column type {t!r}. Use one of Text, Numeric, Int, Bool, Date, Choice, ChoiceList, Any, Ref:<TableId>, RefList:<TableId>.")`.
- Before gating a record write, the connector fetches `list_columns`. A column id not in the table →
  `ValueError(f"Unknown column(s) in table {table_id}: {', '.join(sorted(unknown))}. Call grist_list_tables to see the column ids.")`;
  a formula column → `ValueError(f"Column(s) {', '.join(sorted(cols))} in table {table_id} are formula columns and cannot be written.")`.
- `grist_update_records` fetches `get_records_by_id`; ids not found →
  `ValueError(f"Record id(s) not found in table {table_id}: {', '.join(map(str, sorted(missing)))}.")`.
- `grist_create_table`: a table with that id already in `list_tables` →
  `ValueError(f"Table {table_id} already exists in this document. Use grist_add_columns to add columns to it.")`.
- `grist_add_columns`: the table must exist (else `ValueError(f"Table {table_id} does not exist in this document. Use grist_create_table to create it.")`);
  an existing column id → `ValueError(f"Column(s) {', '.join(sorted(dupes))} already exist in table {table_id}.")`.

Cell display helper `_cell_text(value) -> str`: `None` → `""`; `str` as-is; anything else
`json.dumps(value, ensure_ascii=False)`; result cut to 200 characters with a trailing `…`.

`gated_call` arguments (all keyword; `connector=self.name`; every call passes
`args={"doc_id": doc_id, "table_id": table_id}` plus, for `grist_get_records`, `"filter"`, `"sort"`
and `"limit"`). Every `preview` dict below starts with `"Server": self._client.host`, so the card
always says which Grist server the data comes from or goes to (a bundle can name a self-hosted
server; ADR 0143). The dicts are listed without that first key:

- `grist_get_records` (fetch `_doc_info` and `get_records`, then gate): `tool_name="Read Grist Records"`,
  `summary=f"Read {len(records)} record(s) from {doc.name} / {table_id}"`, `sender=doc.name`,
  `raw_data=page`, `filtered_data=<the return dict>`, `gate="review"`,
  `preview={"Document": doc.name, "Team": doc.team or "(not shown by Grist)", "Table": table_id, "Filter": filter or "(none)", "Sort": sort or "(none)"}`,
  `new_info={"Records": str(len(records)), "Record content": "values of every visible column"}`,
  `details_text` = one block per record (`#<id>` then `<column>: <_cell_text>` lines), or `"(no records)"`,
  `pii_scan_text=details_text`, `preview_tables=[{"headers": ["id", *column ids in first-seen order], "rows": [...]}]`
  (omitted when there are no records), `table_only=True`, `my_email=""`.
  When `truncated`, prefix `details_text` with `f"Showing the first {limit} records; more match.\n\n"`.
- `grist_add_records` (fetch `_doc_info`, `list_columns`, validate, then gate, then write):
  `tool_name="Add Grist Records"`, `summary=f"Add {n} record(s) to {doc.name} / {table_id}"`,
  `sender=doc.name`, `raw_data={"doc_id","table_id","records": rows}`, `filtered_data=None`,
  `gate="popup"`, `preview={"Document": doc.name, "Table": table_id, "Records": str(n)}`,
  `preview_tables=[{"headers": [column ids], "rows": [[_cell_text(...)]]}]`, `details_text` = the
  records as indented JSON.
- `grist_update_records` (fetch `_doc_info`, `list_columns`, `get_records_by_id`, validate, gate,
  write): `tool_name="Update Grist Records"`, `summary=f"Update {n} record(s) in {doc.name} / {table_id}"`,
  `preview={"Document": doc.name, "Table": table_id, "Records": str(n), "Cells changed": str(cells)}`,
  `preview_tables=[{"headers": ["Record", "Column", "Current", "New"], "rows": [[f"#{id}", col, _cell_text(old), _cell_text(new)], ...]}]`,
  `details_text` one line per cell `f"#{id} {col}: {_cell_text(old)} → {_cell_text(new)}"`,
  `raw_data={"doc_id","table_id","records": [...]}`, `filtered_data=None`, `gate="popup"`.
- `grist_create_table`: `tool_name="Create Grist Table"`, `summary=f"Create table {table_id} in {doc.name}"`,
  `preview={"Document": doc.name, "New table": table_id, "Columns": str(n)}`,
  `preview_tables=[{"headers": ["Column", "Label", "Type"], "rows": [...]}]`,
  `details_text` the column lines `f"{id} ({type}) {label}"`, `gate="popup"`, `filtered_data=None`.
- `grist_add_columns`: `tool_name="Add Grist Columns"`, `summary=f"Add {n} column(s) to {doc.name} / {table_id}"`,
  `preview={"Document": doc.name, "Table": table_id, "New columns": str(n)}`, same table and details
  shape as `grist_create_table`, `gate="popup"`, `filtered_data=None`.

A write runs only after `gated_call` returns; a denial raises out of `gated_call` exactly as in every
other connector. Auto tools audit with `_auto_audit("grist_list_documents", "List Grist Documents", "List documents", f"{n} document(s)", t0)`
and `_auto_audit("grist_list_tables", "List Grist Tables", f"List tables of {doc_id}", f"{n} table(s)", t0)`.

### 3.4 Policy tables

| Tool | `TOOL_TO_GATE` | `TOOL_TO_OPERATION` | `TOOL_TO_VERB` | `EFFECT_BY_TOOL` | `_TOOL_LAYOUT` |
|---|---|---|---|---|---|
| `grist_list_documents` | `auto` | — | — | — | — |
| `grist_list_tables` | `auto` | — | — | — | — |
| `grist_get_records` | `review` | `grist.read_records` | `READ` | — | `WIDE` |
| `grist_add_records` | `popup` | `grist.add_records` | `CREATE` | `"New records are added to that table."` | `WIDE` |
| `grist_update_records` | `popup` | `grist.update_records` | `UPDATE` | `"The cells shown change to their new values. Other cells and records are not touched."` | `WIDE` |
| `grist_create_table` | `popup` | `grist.create_table` | `RESTRUCTURE` | `"A new table is added to the document. Nothing existing changes."` | — (NARROW) |
| `grist_add_columns` | `popup` | `grist.add_columns` | `RESTRUCTURE` | `"New, empty columns are added to that table. Existing columns and values do not change."` | — (NARROW) |

Rule scope: `grist.document`, a new **identity** scope on `doc_id`, resolved from the call's args.
It goes in `policy/scopes.NEW_SCOPE_SELECTORS` (`"grist.document": ScopeSelector(predicate="grist.document", scope_type="grist.document", kind=ScopeKind.IDENTITY, resolves_from=ResolvesFrom.ARGS, matches=_grist_document_matches)`,
where `_grist_document_matches` is the `_apps_script_project_matches` shape on `ctx.args.get("doc_id", "")`)
and in `policy/catalogue.EXTRA_SCOPES`:
`"grist.document": PolicyExtraScope(predicate="grist.document", connector="grist", verbs=(Verb.READ, Verb.CREATE, Verb.UPDATE, Verb.RESTRUCTURE), label="Grist — document", needs_value=True, value_hint="8CAN8gKdxY7z…document-id")`.
It gets no `policy/propose.PROPOSABLE_SCOPES` entry, so the approval popup never offers "Always
allow" for a Grist operation; rules are made on **Settings > Auto-accept** or through
`privacyfence_propose_policy_change` (§4, ADR 0144).

### 3.5 Local mode: daemon, Settings and CLI

- `daemon_main.TOKEN_FILES["grist"] = "credentials/grist_token.json"`.
- `build_connectors()`, after the Telegram block:
  ```python
  if enabled("grist"):
      try:
          grist_cfg = grist_oauth_config(org_config.get("grist") or {})
          token_path = _resolve_path(TOKEN_FILES["grist"])
          check_grist_server(load_grist_token(token_path), grist_cfg)
          client = GristClient(grist_cfg.server_url, GristTokenProvider(grist_cfg, token_path))
          client.check_connection()
          connectors.append(GristConnector(client))
      except GristClientError as exc:
          logger.warning("Grist connector disabled: %s", exc)
          failures["grist"] = _classify_connector_failure(exc)
  ```
  (`grist_oauth_config`, `load_grist_token` and `check_grist_server` are `grist_oauth.oauth_config`,
  `load_token_file` and `check_server_matches` imported under those names, like
  `load_salesforce_token`.)
- `run_grist_oauth(org_config) -> int` and a `--grist-oauth` flag, in the shape of
  `run_salesforce_oauth` (`:1786-1802`) and its flag/dispatch: no usable section → print
  `"No Grist organization config installed."` to stderr, return 1; success → print
  `f"Grist OAuth complete. Signed in to {host}."`, return 0; `GristClientError` → print
  `f"Grist OAuth setup failed: {exc}"`, return 1.
- `settings_controller.py`: `"grist"` appended to `ALL_CONNECTORS`; `ORG_CONFIG_SERVICE["grist"] = "grist"`;
  `"grist"` appended to `ORG_BUNDLE_SERVICES`; `authenticate_connector` gains
  `elif connector == "grist": self._authenticate_grist(org_config)`; `_authenticate_grist` is
  `_authenticate_salesforce` (`:1331-1357`) with `grist_oauth.oauth_config` (its
  `GristClientError` → `self.error = "Grist organization config isn't installed yet."`) and
  `grist_oauth.authorize_interactive`, error text `f"Grist authentication failed: {result}"`.
  `connector_label("grist")` is already `"Grist"`. The page's generic Authenticate… button needs no
  change.
- `scripts/qa_authenticate_connectors.py` `STEPS`: `OAuthStep("grist", "grist", "--grist-oauth", "Grist")`
  after Salesforce; its docstring and `--only` help name the new group.

### 3.6 Org mode

- Bundle section `grist` written by `scripts/build_org_bundle.py`: a "Grist" argument group with
  `--grist-server-url` (default `https://docs.getgrist.com`), `--grist-client-id`,
  `--grist-client-secret` (both or neither, as Salesforce's pair) and `--grist-auth-server-url`
  (optional; written only when given). The script stays standard-library only: it strips a
  trailing `/` from both URLs and rejects (`SystemExit("--grist-server-url and --grist-auth-server-url must be https:// addresses (http:// only for localhost).")`)
  a URL with an empty host, or a scheme other than `https` except `http` with host `localhost`,
  `127.0.0.1` or `::1` (the same rule as `normalize_server_url`). `"grist"` joins the
  `services` tuple at `:692`, and `_CONNECTOR_CALLBACKS["grist"] = ("grist",)` so the org-mode
  summary prints `{issuer}/oauth/callback/grist`.
- `web/routes_connect.py`: `"grist"` in `OAUTH_SERVICES`, `_GRANT_KEY`, `SERVICE_LABELS`
  (`"Grist"`), `_ORG_CONFIG_SECTION`; `_is_configured` is true when `oauth_config` accepts the
  section; `_build_authorize_url` and `_exchange_and_save` get a `grist` branch in the Salesforce
  shape (`org_identity.generate_pkce_pair()`, `grist_oauth.discover(cfg.auth_server_url, cfg.server_url)`
  — called synchronously like the other branches' provider calls; it is cached per URL after the first sign-in,
  `build_authorize_url`, `exchange_code`, `save_token_file`); the rows tuple in
  `_render_connect_page` becomes `("slack", "salesforce", "jira", "confluence", "grist")`.
  `tests/unit/web/test_routes_connect.py:170` asserts the row count, which becomes 12.
- Org redirect URI: `https://<server>/oauth/callback/grist`. One Grist app can carry it and the
  local one, `http://localhost:53685/callback`, as two redirect URIs.

### 3.7 What is deliberately not built

- No delete of records, columns or tables; no column rename, modify or type change; no SQL tool; no
  attachments; no `doc:download`, `doc:webhooks` or `user.profile:read` scope. Adding any of them
  later is a new decision.
- No pasted API key, in either mode (ADR 0142).
- No connector icon (only real brand assets go in `resources/connector_icons/`).

### 3.8 ADR-worthy decisions (written in the last phase)

See §4.

### 3.9 Setup guide `grist-setup.md` (in `docs/`)

Modelled on `docs/salesforce-setup.md`. Sections: `# Grist setup`; `## What you need` (a Grist
account on getgrist.com, or a self-hosted Grist with OAuth apps; an administrator registers one
app); `## Register the app` (Grist → profile picture → **Account settings** → **Developer** →
**OAuth apps** → **Register app**; name; redirect URIs `http://localhost:53685/callback` and, for an
organization server, `https://<server>/oauth/callback/grist` on its own line; permissions
`doc:read`, `doc:write`, `doc.schema:write`, `offline_access`; a sentence that Grist says
`doc.schema:write` can reveal any data in a document through formulas and that PrivacyFence uses
it only to add tables and columns); `## Values` (table: server address, client id, client secret,
sign-in server, with the `build_org_bundle.py` option for each; the sign-in server is left unset
for getgrist.com and for any server that serves `/.well-known/oauth-authorization-server` itself); `## Build and distribute the bundle`;
`## Users connect` (Authenticate… in Settings, or Connect on the connections page); `## What the
assistant can do` (the seven tools and their gates; link to the tools reference `#grist`; some
servers do not let apps list documents, and then the assistant asks for the document id from its
address); `## Auto-accept rules` (`grist.document`, set on Settings > Auto-accept; no Always allow
on the card); `## Troubleshooting` (the exact error texts from §3.2.1 and §3.2.2 with what to do).

## 4. ADRs

- **0142** — Grist sign-in is OAuth (authorization code + PKCE, confidential client,
  `offline_access`), with the app registered by an administrator and carried in the organization
  bundle, as for Salesforce and Atlassian; the token file is per principal
  (`credentials/grist_token.json`, `atomic_write_json`, 0600). The app requests `doc:read`,
  `doc:write`, `doc.schema:write` and `offline_access`, knowingly: Grist documents that
  `doc.schema:write` can reveal any data through formulas, and the connector uses it only to add
  tables and columns. Rejected: a pasted personal API key (full account access with no scopes, and a
  new kind of typed secret in Settings and on `/connect`); dropping the schema tools to avoid
  `doc.schema:write`. Consequences: a self-hosted Grist without OAuth apps cannot connect; `grist`
  becomes a reserved plugin name, so a plugin already called `grist` is refused.
- **0143** — The Grist server and its sign-in server come only from the bundle, never from a
  person; a token is used only with the server it was issued for (a mismatch asks to sign in
  again); only `https` (or `http` to loopback); redirects are never followed; discovery starts at
  the configured server, follows the issuer it names at most once (that issuer must confirm itself),
  and accepts only endpoints on the issuer's host, so the client secret and refresh tokens go only
  where the configured server points; every Grist approval card names the server. Rejected: a user-entered server address in local mode (an OAuth app is
  registered per server anyway, and a free address lets data go to any server).
- **0144** — Grist auto-accept rules are scoped per document (`grist.document`) and made on
  Settings or through the bridge tool, never from the approval card's "Always allow". Rejected:
  adding the scope to `SCOPE_SELECTORS`, which is the frozen v1-equivalence set
  (`tests/unit/policy/_v1_reference.py`); the popup proposes only from that set (ADR 0077 is the
  precedent).
- **0145** — The Grist connector only adds: records, tables and columns. Nothing deletes, renames or
  changes a column's type. Rejected: `grist_delete_records` and schema-modify tools (a delete
  cannot be previewed as anything but ids, and a type change can rewrite every value in a column).

## 5. Manual steps

Step-by-step page: see `manual_steps_artifact` in the manifest.

- **Before implementation**: create a Grist QA account, a seed document (tables `QaSeed` and
  `QaLifecycle`), register the
  PrivacyFence QA app in Grist and note where its discovery document is served
  (`mb1-grist-qa-account`); on the self-hosted runner, add the app to the QA bundle, the seed ids
  to `qa_environment.yaml`, and create the token file with the bootstrap script on the page
  (`mb2-runner-qa-state`). `p10-qa-recorder` dispatches `qa-record-fixture.yml`, which fails
  without them.
- **After implementation**: sign in and drive every Grist tool from a real AI client in local mode
  (`ma1-local-mode-check`), and, if an org-mode test deployment exists, the `/connect` sign-in
  (`ma2-org-mode-check`).

## 6. Risks and open questions

- **OAuth details not yet seen live.** §3.1 comes from Grist's help, API reference and the
  getgrist.com discovery document (seen live). Unverified: whether
  `prompt=consent` plus `offline_access` returns a refresh token on every sign-in, and the exact
  token response fields. `mb2`'s bootstrap script exercises discovery, the authorize URL and the
  code exchange on the real server before any phase starts; if it fails, the user reports the
  error and the plan is revised before `/implement` continues. If a phase still meets a real
  response that contradicts §3.2.1, stop with `status=blocked`.
- **Account-level endpoints under OAuth.** If `GET /api/orgs` and `GET /api/docs/{docId}` refuse
  OAuth tokens, `grist_list_documents` reports the §3.3 message and cards show the document id
  instead of its name. That is designed behaviour, not a failure. If instead the table, column or
  record endpoints refuse the token, stop with `status=blocked`.
- **Grist response shapes.** The parsers tolerate missing optional keys (empty string).
  `p10-qa-recorder` records real responses; a parser a fixture contradicts is fixed in that
  phase. If a recorded shape makes a §3.3 behaviour impossible, stop with `status=blocked`.
- **`grist.document` and the popup.** If after `p7-policy-scope` the approval card offers an
  "Always allow" for a Grist tool, stop with `status=blocked`.
- **Tests that enumerate connectors.** `tests/unit/test_daemon_main.py`,
  `test_settings_controller.py`, `test_qa_authenticate_connectors.py` and `web/test_routes_connect.py`
  may pin the exact connector list or a count. Update those to include `grist`; if one asserts
  something this plan does not account for, stop with `status=blocked`.
- **Website goes live on merge.** `pages.yml` deploys `website/` from `main`, so
  `/connectors/grist/` is public once the feature PR merges, before a release carries the
  connector. Its "Set it up" button links the guide on GitHub's `main`. Holding the page back
  would be a change to `p3-connector-listing` only.
- **Counts on the website.** Each tool phase changes the totals in `website/connectors/index.html`,
  `website/how-it-works/index.html` and the Grist card; always take them from the regenerated
  `docs/tools-reference.md` summary table.
- **The runner credential.** If the dispatched `qa-record-fixture.yml` fails at its "copy QA state"
  step, with "Grist organization config not installed", or with "Grist is not authenticated",
  `mb2-runner-qa-state` is not done: stop with `status=blocked` and say so.

## Implementation manifest

```yaml
plan_slug: grist-connector
feature_branch: feature/grist-connector
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/RejYXshpnB1gBwT6ySJQNH
manual_steps_source: docs/grist-connector-plan-manual-steps.html
manual_before:
  - id: mb1-grist-qa-account
    title: Create a Grist QA account and seed document, and register the PrivacyFence QA OAuth app in Grist
    why: p10-qa-recorder records live fixtures from this document through this app; without them the recording has nothing to read and no way to sign in.
    done_when: A Grist document "PrivacyFence QA [QATEST]" has a table QaSeed (columns Name, Note) with two [QATEST] rows and an empty table QaLifecycle (columns Name, Note), a contrast document exists, an OAuth app "PrivacyFence QA" with redirect URI http://localhost:53685/callback and the scopes doc:read, doc:write, doc.schema:write and offline_access exists, its client id and secret are in your password manager, and you know which address serves /.well-known/oauth-authorization-server.
  - id: mb2-runner-qa-state
    title: Add the Grist app to the runner's QA bundle, the seed ids to qa_environment.yaml, and create the runner's Grist token with the bootstrap script
    why: p10-qa-recorder dispatches qa-record-fixture.yml, which reads the bundle's grist section, ~/privacyfence/credentials/grist_token.json and the grist section of qa_environment.yaml on the runner. The bootstrap script also proves discovery, the authorize URL and the code exchange against the real Grist before any phase starts.
    done_when: On the runner, ~/privacyfence/org/org_config.json has a grist section (server_url, client_id, client_secret, and auth_server_url if discovery is not served by the server itself), ~/privacyfence/credentials/grist_token.json exists with mode 600 and the keys server_url, access_token, refresh_token and expires_at, and ~/privacyfence/tests/fixtures/qa_environment.yaml has a grist section with doc_id, table_id and lifecycle_table_id. If the bootstrap script failed, report its error instead and do not start /implement.
manual_after:
  - id: ma1-local-mode-check
    title: Sign in to Grist from Settings and drive every Grist tool from an AI client
    why: Proves the real OAuth sign-in, the approval cards' content (server, values, old→new diffs) and real writes against Grist, which unit tests and the recorder cannot show.
  - id: ma2-org-mode-check
    title: (If you run an org-mode test deployment) sign in to Grist on /connect with a bundle that carries the app
    why: Proves the bundle section, the org redirect URI and the per-person sign-in; no CI job runs an org deployment against Grist.
verify_after_merge:
  - python3 -m pytest tests/unit/test_grist_oauth.py tests/unit/test_grist_client.py tests/unit/test_systemic_gate_invariants.py -q
  - python3 -m pytest tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_website_docs_allowlist.py tests/unit/test_connector_tool_annotations.py -q
  - python3 -m pytest tests/unit/connectors -q -k grist
  - python3 -m pytest tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_generate_always_allow_reference.py -q
  - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/test_qa_authenticate_connectors.py tests/unit/web/test_routes_connect.py tests/unit/test_build_org_bundle.py tests/unit/test_qa_fixture_recorder.py -q
final_checks:
  - docs/grist-connector-plan.md and docs/grist-connector-plan-manual-steps.html are deleted and nothing links to them (grep -rn "grist-connector-plan" . --exclude-dir=.git finds nothing)
  - ADRs 0142, 0143, 0144 and 0145 exist in docs/adr/, are Accepted, and are in the docs/adr/README.md index
  - CHANGELOG.md has the Grist entry under "## [Unreleased]" and no new version heading
  - After python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py, git diff --exit-code docs/tools-reference.md docs/always-allow-rules-reference.md exits 0
  - The PR description links the qa-record-fixture.yml run (p10-qa-recorder) and the connector-live-check.yml run (p11-docs-adrs-retire), which is the definition-of-done QA row
  - No file under src/ or scripts/ reads or stores a Grist API key (grep -rni "api_key" src/privacyfence/grist_*.py src/privacyfence/connectors/grist.py finds nothing)
phases:
  - id: p0-reserve-name
    title: Reserve the plugin name grist in the daemon, the SDK, the protocol schema and its doc
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/plugins/constants.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - docs/plugin-protocol/protocol.schema.json
      - docs/plugin-protocol.md
    brief: |
      Read first: the must-read docs and plan §3.0. A new connector's name is reserved so no plugin can
      take it; the list exists in four copies that tests keep equal.
      1. src/privacyfence/plugins/constants.py RESERVED_PLUGIN_NAMES (l.104-109): add "grist" after "telegram".
      2. plugin-sdk/src/privacyfence_plugin_sdk/plugin.py _RESERVED_PLUGIN_NAMES (l.73-78): the same.
      3. docs/plugin-protocol/protocol.schema.json: add "grist" to $defs.Manifest.properties.name.not.enum
         (after "telegram", near l.554). Do not change "x-protocol-version".
      4. docs/plugin-protocol.md (l.146-148): add `grist` to the list of connector names after `telegram`.
      5. grep -rn '"telegram"' plugin-sdk docs/plugin-protocol* src/privacyfence/plugins and add "grist" to any
         other copy of this list the grep finds; name it in your report.
      6. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if a test demands a protocol or SDK version bump for this change, stop with status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/plugin_sdk/test_plugin.py tests/unit/plugins/test_protocol.py -q passes
      - 'grep -n ''"grist"'' src/privacyfence/plugins/constants.py plugin-sdk/src/privacyfence_plugin_sdk/plugin.py docs/plugin-protocol/protocol.schema.json matches in all three files'
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p1-oauth
    title: Grist OAuth module (discovery, PKCE sign-in, refresh, token file) and its tests
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/grist_oauth.py
      - tests/unit/test_grist_oauth.py
      - tests/unit/test_systemic_gate_invariants.py
      - tests/conftest.py
    brief: |
      Read first: the must-read docs, plan §3.0, §3.1 and §3.2.1 (the spec), and the Salesforce OAuth functions
      this copies in shape: src/privacyfence/salesforce_client.py:415-530 and oauth_loopback.run_browser_oauth (l.148).
      1. Create src/privacyfence/grist_oauth.py exactly as plan §3.2.1: module docstring, GristClientError and
         GristAccessDenied, the constants, normalize_server_url, GristOAuthConfig and oauth_config, GristOAuthEndpoints
         and discover (with its module-level cache), build_authorize_url, exchange_code, refresh, save_token_file,
         load_token_file, check_server_matches, GristTokenProvider and authorize_interactive. All HTTP through
         requests with timeout=30 and allow_redirects=False. Exact error strings from §3.2.1. Never log a token,
         the client secret or a code; log the host only.
      2. tests/conftest.py _reset(): clear grist_oauth's discovery cache (one line, next to the other module resets).
      3. tests/unit/test_grist_oauth.py (pytestmark = pytest.mark.unit; module docstring naming the invariant "a Grist
         token is only ever used with the server it was issued for"). Fake requests at the boundary with monkeypatch
         (no network). Classes: TestNormalizeServerUrl (every rule and message in §3.2.1), TestOAuthConfig (defaults
         auth_server_url to server_url; missing keys → "organization config not installed"), TestDiscover (parses the
         endpoints; client_secret_basic True when the methods key is absent or lists client_secret_basic, False when it
         lists only client_secret_post; http endpoint on a non-loopback host rejected; a redirect rejected; cached
         per URL; the getgrist.com shape from plan §3.1 as a fixture: discovery at the server names issuer
         https://login.getgrist.com/, the issuer's own document is fetched once and its endpoints used; an issuer whose
         own document names a different issuer is rejected; an endpoint on a host other than the issuer's is
         rejected), TestBuildAuthorizeUrl (every parameter, scope string exactly GRIST_SCOPES, prompt=consent,
         S256), TestExchangeCode (Basic auth header vs form secret per discovery; record shape with server_url and
         expires_at; no refresh_token → its message; 400 with error/error_description → its message),
         TestRefresh (new refresh_token replaces the old; absent keeps the old; invalid_grant → the expired message),
         TestTokenFile (round trip; mode 0o600 on POSIX; missing, incomplete, invalid JSON and non-object files),
         TestCheckServerMatches, TestGristTokenProvider (no refresh while valid; refresh 60 s before expiry and on
         force_refresh; the refreshed record is saved), TestAuthorizeInteractive (run_browser_oauth monkeypatched;
         port 53685, path /callback, redirect_host localhost; OAuthLoopbackError → "Grist sign-in failed: …").
         Assert no token or secret appears in any raised message.
      4. tests/unit/test_systemic_gate_invariants.py: add ("grist_oauth", None, "save_token_file") to TOKEN_WRITE_SITES and
         rename test_ten_token_write_sites_are_listed to test_eleven_token_write_sites_are_listed asserting len == 11.
      5. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if oauth_loopback.run_browser_oauth's signature differs from (build_authorize_url, exchange, port,
      path, timeout, open_browser, redirect_host), stop with status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/test_grist_oauth.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit/test_grist_oauth.py -q --cov=privacyfence.grist_oauth --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/grist_oauth.py
      - 'grep -n "GRIST_SCOPES = \"doc:read doc:write doc.schema:write offline_access\"" src/privacyfence/grist_oauth.py matches'
      - grep -n "verify=False" src/privacyfence/grist_oauth.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p2-client
    title: Grist REST client and its tests
    depends_on: [p1-oauth]
    complexity: M
    touches:
      - src/privacyfence/grist_client.py
      - tests/unit/test_grist_client.py
    brief: |
      Read first: plan §3.0, §3.1 and §3.2.2 (the spec), and src/privacyfence/grist_oauth.py from the previous phase.
      1. Create src/privacyfence/grist_client.py exactly as plan §3.2.2: the re-exported errors, dataclasses, id
         validation, GristClient with host, _request (the single choke point: bearer from the token provider, one
         force_refresh retry on 401, the error mapping including GristAccessDenied for 403), check_connection (no REST
         call) and every public method. Exact error strings from §3.2.2. Never log a token or a cell value.
      2. tests/unit/test_grist_client.py (pytestmark unit; module docstring). Build clients on a fake token provider
         (an object with access_token(*, force_refresh=False) returning "at-1" then "at-2" after a forced refresh) and
         monkeypatch the client's requests.Session.request (no network). Classes: TestValidateIds (doc id with ~
         accepted; "../x", "a/b", "a?b" rejected; table/column regex), TestRequestErrors (connection error, 302,
         401 then success after one forced refresh, 401 twice, 403 raises GristAccessDenied, 404, 500 with
         {"error": ...} cut to 200 chars, 500 without JSON, 200 with an HTML body — exact messages; assert
         allow_redirects=False and the Authorization header; no token in any message), TestCheckConnection (returns the
         host and makes no HTTP request), TestHost, and one class per public method asserting method, path,
         params/body and parsing: TestListDocuments (the document id is used even when urlId is present, sorting, org
         cap 20), TestGetDocument (missing workspace/org keys give ""), TestListTables, TestListColumns (label falls
         back to id), TestGetRecords (limit+1 requested, truncated flag, filter JSON-encoded), TestGetRecordsById,
         TestAddRecords, TestUpdateRecords, TestAddTable, TestAddColumns. Each id-taking method rejects a bad id
         before any request.
      3. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/test_grist_client.py -q passes
      - python3 -m pytest tests/unit/test_grist_client.py -q --cov=privacyfence.grist_client --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/grist_client.py
      - grep -n "allow_redirects=False" src/privacyfence/grist_client.py matches
      - grep -n "verify=False" src/privacyfence/grist_client.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p3-connector-listing
    title: GristConnector with the two auto listing tools, and every per-connector table, page and guide a new connector module requires
    depends_on: [p0-reserve-name, p2-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/grist.py
      - tests/unit/connectors/test_grist_connector.py
      - src/privacyfence/auto_accept.py
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - scripts/build_site.py
      - docs/tools-reference.md
      - docs/grist-setup*.md
      - docs/README.md
      - README.md
      - website/connectors/grist/index.html
      - website/_partials/other-connectors.html
      - website/connectors/index.html
      - website/how-it-works/index.html
      - website/compare/mcp-gateways/index.html
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
      - tests/unit/test_website_connectors_page.py
    brief: |
      Read first: the must-read docs, plan §3.3 (connector spec), §3.9 (setup guide), and
      src/privacyfence/connectors/salesforce.py (template for _fetch, _auto_audit, tool_specs style).
      1. Create src/privacyfence/connectors/grist.py: module docstring, GristConnector(Connector) with
         name "grist", __init__(client: GristClient), client property, _fetch and _auto_audit as plan §3.3,
         tool_specs() returning ONLY grist_list_documents and grist_list_tables with the descriptions,
         params and sibling names from §3.3, and call() dispatching them (unknown tool →
         ValueError(f"Unknown Grist tool: {tool!r}")). Return shapes per the §3.3 table. Leave room for the
         other five tools (later phases add them); do not add them now.
      2. src/privacyfence/auto_accept.py TOOL_TO_GATE: "grist_list_documents": "auto", "grist_list_tables": "auto"
         (with a "# Grist" comment line like the other connectors' groups).
      3. Bookkeeping ("grist" is already a reserved plugin name, from p0-reserve-name):
         "privacyfence.connectors.grist" to scripts/pyinstaller_common.py's list; "grist": "Grist" to both
         CONNECTOR_TITLES and CONNECTOR_SHORT in scripts/generate_tools_reference.py (after "confluence");
         GristConnector to CONNECTOR_CLASSES (and its import) in tests/unit/connectors/test_readme_manifest_alignment.py.
      4. Run python3 scripts/generate_tools_reference.py to regenerate docs/tools-reference.md (Grist row:
         2 tools, 2 auto, 0 review, 0 popup).
      5. Website and docs, required by tests/unit/test_website_connector_pages.py and test_website_connectors_page.py:
         a. The setup guide grist-setup.md in docs/, with the sections in plan §3.9 (describe all seven tools and both modes now;
            the feature ships as a whole).
         b. README.md "## Connectors" table: row "| Grist | List documents and tables; read records after review; add and update records, add tables and columns (nothing is deleted) |" after Confluence.
         c. website/connectors/grist/index.html, modelled on website/connectors/telegram/index.html (same head,
            meta pf-content-group connector, canonical/og URLs for /connectors/grist/). Copy: list straight away,
            read records after review, add and change only with approval, works with docs.getgrist.com, team
            sites and self-hosted Grist with OAuth apps, you sign in with your own Grist account. Its "Set it up" button is exactly
            <a class="button primary" href="GUIDE_URL">Set it up</a>.
            where GUIDE_URL is https://github.com/privacyfence/privacyfence/blob/main/ followed by docs/ and the guide's
            file name grist-setup.md, with no space (written split here because test_docs_references_exist.py
            rejects a plan naming a doc path that does not exist yet).
         d. scripts/build_site.py: "/connectors/grist/": "connectors/grist/index.html" in PAGES (after telegram)
            and "grist-setup" in CONNECTOR_GUIDES.
         e. tests/unit/test_website_connector_pages.py CONNECTORS: "grist": ("/connectors/grist/", "grist-setup", "Grist").
         f. website/connectors/index.html: a Grist card in the same position as Grist's row in the
            tools-reference summary table (last), copying the Telegram card's markup with id="grist",
            data-connector="Grist", data-tools="2" data-auto="2" data-review="0" data-popup="0", the printed line
            "2 tools: 2 without a card · 0 reviewed · 0 need approval", links to /docs/tools-reference/#grist and
            /connectors/grist/; change the heading to "Twelve connectors, 122 tools, each with a fixed gate."
         g. website/how-it-works/index.html: "120 connector tools" → "122 connector tools".
         h. website/compare/mcp-gateways/index.html: "Its own eleven connectors" → "Its own twelve connectors".
         i. tests/unit/test_website_connectors_page.py: len(REFERENCE) == 12 and "Twelve connectors".
         j. website/_partials/other-connectors.html: add
            <li data-connector="grist"><a href="/connectors/grist/">Grist</a></li> after the Telegram line, and
            update the partial's header comment ("the other four") to the new count. The Grist page includes
            the partial with current="grist" exactly as the Telegram page does with current="telegram";
            scripts/build_site.py's _without_current_connector (l.304-310) raises BuildError without that line.
         k. docs/README.md: add "- [`grist-setup.md`](grist-setup.md)" after the telegram-setup.md line in the
            user-and-operator half's connector guide list (l.46-51), so test_website_docs_allowlist.py passes.
      6. Create tests/unit/connectors/test_grist_connector.py (module docstring; pytestmark unit): TestDispatch
         (unknown tool → ValueError); TestListDocuments (including GristAccessDenied → the §3.3 refusal message) and TestListTables (never call gated_call — use the
         gated_call_spy fixture pattern from tests/unit/connectors/test_salesforce_connector.py:75-84 and assert it
         stays empty; each writes an auto_accepted audit entry with the tool name; a GristClientError becomes
         RuntimeError; a bad doc_id raises before the client is called); TestToolDefinitions with
         GRIST_SIBLINGS = {"grist_list_documents": ["grist_list_tables"], "grist_list_tables": ["grist_list_documents"]}
         (shape as SALESFORCE_SIBLINGS) calling assert_tool_definitions_complete; TestEveryToolIsAudited calling
         assert_all_tools_leave_an_audit_trail.
      7. Run ruff check . and python3 -m pytest tests/unit -q (the whole unit suite: many cross-connector tests discover
         the new module), and python3 scripts/build_site.py --out /tmp/pf-site-check --no-docs --offline to confirm the
         site builds and its link check passes.
      Stop condition: if a unit test outside the files in touches fails because of the new connector module
      (another hand-maintained connector list this plan does not name), stop with status=blocked and name the test.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q passes
      - python3 -m pytest tests/unit/test_website_docs_allowlist.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_pyinstaller_hidden_imports.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_connector_tool_annotations.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit -q passes
      - grep -n "| \[Grist\](#grist) | 2 | 2 | 0 | 0 |" docs/tools-reference.md matches
      - python3 scripts/build_site.py --out /tmp/pf-site-check --no-docs --offline exits 0
      - ruff check . passes
  - id: p4-get-records
    title: grist_get_records, the review-gated read
    depends_on: [p3-connector-listing]
    complexity: S
    touches:
      - src/privacyfence/connectors/grist.py
      - tests/unit/connectors/test_grist_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - docs/always-allow-rules-reference.md
    brief: |
      Read first: plan §3.3 (grist_get_records row, filter/sort/limit validation, _cell_text, its gated_call
      arguments) and §3.4; src/privacyfence/connectors/salesforce.py _get_record (l.403-447) as the pattern.
      1. In connectors/grist.py add grist_get_records to tool_specs() (description, params, sibling
         grist_list_tables) and call(); implement the filter/sort/limit validation helpers and _cell_text exactly
         as §3.3; implement _doc_info (§3.3), fetch it and get_records through _fetch, then call gated_call with exactly the §3.3
         arguments including pii_scan_text=details_text; return the §3.3 dict. Update GRIST_SIBLINGS in the test
         (grist_list_tables also lists grist_get_records; grist_get_records lists grist_list_tables).
      2. auto_accept.py: TOOL_TO_GATE "grist_get_records": "review"; TOOL_TO_OPERATION "grist_get_records":
         "grist.read_records". policy/registry.py TOOL_TO_VERB "grist_get_records": Verb.READ. gate.py
         _TOOL_LAYOUT "grist_get_records": WIDE.
      3. python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py (it lists
         every review and popup tool); then website/connectors/index.html Grist card →
         data-tools="3" data-auto="2" data-review="1" data-popup="0", printed line
         "3 tools: 2 without a card · 1 reviewed · 0 need approval", heading total 123 tools;
         website/how-it-works/index.html "123 connector tools".
      4. Tests in tests/unit/connectors/test_grist_connector.py: TestGetRecords — preview dict has exactly the
         keys Server, Document, Team, Table, Filter, Sort (Server first, equal to client.host) and no cell value (data minimization); details_text carries the
         cells; pii_scan_text == details_text; preview_tables headers start with "id"; args carry doc_id,
         table_id, filter, sort, limit; truncated prefix; empty result gives "(no records)" and no table; get_document raising GristAccessDenied gives Document = the doc id and Team = "(not shown by Grist)"; each
         filter/sort/limit ValueError message (gated_call_spy stays empty); GristClientError → RuntimeError.
         TestFieldCompleteness — a real GristClient with a faked _request returning a fully populated
         /api/docs/{id} and records response, run through grist_get_records, checked with
         assert_no_placeholder_fields(gated_call_spy[0]["preview"]) (pattern:
         tests/unit/connectors/test_confluence_connector.py TestFieldCompleteness).
      5. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py tests/unit/policy/test_registry.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Grist\](#grist) | 3 | 2 | 1 | 0 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p5-record-writes
    title: grist_add_records and grist_update_records, popup-gated
    depends_on: [p4-get-records]
    complexity: M
    touches:
      - src/privacyfence/connectors/grist.py
      - tests/unit/connectors/test_grist_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - docs/always-allow-rules-reference.md
    brief: |
      Read first: plan §3.3 (records validation, column checks, the two tools' gated_call arguments) and §3.4;
      the popup patterns in src/privacyfence/connectors/contacts.py:305-342 and connectors/jira.py:806-839.
      1. connectors/grist.py: add grist_add_records and grist_update_records (specs, call(), siblings of each
         other). Order inside each: parse and validate args (ValueError) → _doc_info and _fetch list_columns
         (and get_records_by_id for update) → unknown/formula column and missing-id checks (ValueError) →
         gated_call(gate="popup", …) with the §3.3 arguments → the client write through _fetch → return the §3.3
         dict. The write must not run if gated_call raises.
      2. Tables: auto_accept TOOL_TO_GATE popup ×2; TOOL_TO_OPERATION grist.add_records / grist.update_records;
         registry TOOL_TO_VERB CREATE / UPDATE; write_effects.EFFECT_BY_TOOL with the two exact strings in §3.4
         (under a "# ── Grist ──" header like the others); gate._TOOL_LAYOUT WIDE for both.
      3. Regenerate docs/tools-reference.md (python3 scripts/generate_tools_reference.py) and
         docs/always-allow-rules-reference.md (python3 scripts/generate_always_allow_reference.py); website card → 5 tools: 2 auto, 1 review, 2 popup
         ("5 tools: 2 without a card · 1 reviewed · 2 need approval"), heading 125 tools; how-it-works 125.
      4. Tests: TestAddRecords and TestUpdateRecords — preview dict holds only the §3.3 keys; preview_tables
         shape; update's Current/New table and "→" details lines; gate == "popup"; filtered_data is None; the
         client write is called only after the spy returns and not at all when the spy raises
         (monkeypatch gated_call to raise RuntimeError); every validation message in §3.3 for records,
         unknown column, formula column, missing ids, duplicate ids — and gated_call_spy stays empty for each;
         added_ids/updated_ids returned. Extend GRIST_SIBLINGS.
         TestEveryToolIsAudited: build the connector on a MagicMock client whose get_document returns
         GristDocument("DOC1", "QA doc", "Home", "Personal"), list_columns returns [GristColumn("Name", "Name",
         "Text", False)], list_tables returns [GristTable("Table1", [GristColumn("Name", "Name", "Text", False)])],
         get_records_by_id returns [GristRecord(1, {"Name": "old"})], add_records returns [2], host is
         "docs.getgrist.com"; and pass arg_overrides {"grist_add_records": {"doc_id": "DOC1", "table_id": "Table1",
         "records": '[{"Name": "a"}]'}, "grist_update_records": {"doc_id": "DOC1", "table_id": "Table1",
         "records": '[{"id": 1, "fields": {"Name": "b"}}]'}} plus doc_id/table_id overrides for the read tools.
      5. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q passes
      - python3 -m pytest tests/unit/test_write_effects.py tests/unit/policy/test_registry.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Grist\](#grist) | 5 | 2 | 1 | 2 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p6-schema-writes
    title: grist_create_table and grist_add_columns, popup-gated
    depends_on: [p5-record-writes]
    complexity: M
    touches:
      - src/privacyfence/connectors/grist.py
      - tests/unit/connectors/test_grist_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - docs/tools-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - docs/always-allow-rules-reference.md
    brief: |
      Read first: plan §3.3 (columns validation, type allowlist, the two tools' gated_call arguments), §3.4, §3.7.
      1. connectors/grist.py: add grist_create_table and grist_add_columns (specs, call(), siblings of each other)
         with the same ordering rule as the record writes: validate → _doc_info and _fetch list_tables
         (create_table) or list_columns via list_tables (add_columns) → existence checks → gated_call(gate="popup")
         → write → return the §3.3 dict (create_table returns the table id Grist assigned).
      2. Tables: TOOL_TO_GATE popup ×2; TOOL_TO_OPERATION grist.create_table / grist.add_columns; TOOL_TO_VERB
         RESTRUCTURE ×2; EFFECT_BY_TOOL with the exact §3.4 strings. No _TOOL_LAYOUT entry (NARROW default).
      3. Regenerate docs/tools-reference.md (python3 scripts/generate_tools_reference.py) and
         docs/always-allow-rules-reference.md (python3 scripts/generate_always_allow_reference.py); website card → 7 tools: 2 auto, 1 review, 4 popup
         ("7 tools: 2 without a card · 1 reviewed · 4 need approval"), heading 127 tools; how-it-works 127.
      4. Tests: TestCreateTable and TestAddColumns — preview keys, the Column/Label/Type table, default type Text,
         every type in the allowlist accepted (parametrize), Ref:Bad-Id and "DateTime" rejected with the exact
         message, existing table / missing table / existing column messages, no write when the gate raises.
         Extend GRIST_SIBLINGS. In TestEveryToolIsAudited's fake client, add_table returns "NewTable" and add_columns
         returns ["Count"]; arg_overrides add {"grist_create_table": {"doc_id": "DOC1", "table_id": "NewTable",
         "columns": '[{"id": "Title"}]'}, "grist_add_columns": {"doc_id": "DOC1", "table_id": "Table1",
         "columns": '[{"id": "Count", "type": "Int"}]'}}.
      5. Confirm no Grist tool sets destructive (test_connector_tool_annotations.py unchanged and passing).
      6. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q passes
      - python3 -m pytest tests/unit/test_write_effects.py tests/unit/policy/test_registry.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Grist\](#grist) | 7 | 2 | 1 | 4 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q --cov=privacyfence.connectors.grist --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/connectors/grist.py
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p7-policy-scope
    title: The grist.document auto-accept scope, offered on Settings and the bridge but never by the popup
    depends_on: [p6-schema-writes]
    complexity: S
    touches:
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/policy/registry.py
      - docs/always-allow-rules-reference.md
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_propose.py
      - tests/unit/policy/test_registry.py
      - tests/unit/test_gate.py
    brief: |
      Read first: plan §3.4 "Rule scope", and how apps_script.project is wired: policy/scopes.py:515-572,
      policy/catalogue.py:1-60, the propose.py docstring's "What the popup deliberately does not propose",
      tests/unit/policy/test_scopes.py:395-420, tests/unit/policy/test_catalogue.py:20-70, tests/unit/test_gate.py:1040-1060.
      1. scopes.py: add _grist_document_matches (the _apps_script_project_matches shape on ctx.args["doc_id"]) and
         the "grist.document" entry in NEW_SCOPE_SELECTORS exactly as §3.4; extend the "Scope types with no old
         predicate" comment and the module docstring's NEW_SCOPE_SELECTORS sentence to name grist.document.
      2. catalogue.py: add the "grist.document" PolicyExtraScope exactly as §3.4; update the docstrings that say
         EXTRA_SCOPES covers "three operation groups" to name Grist's five operation keys too.
      3. propose.py docstring "What the popup deliberately does not propose": add a bullet that grist.read_records,
         add_records, update_records, create_table and add_columns are governed by grist.document, a scope with no
         entry here. registry.py docstring's sentence listing what policy/catalogue.py offers: add grist.document.
         No PROPOSABLE_SCOPES entry.
      4. python3 scripts/generate_always_allow_reference.py to regenerate docs/always-allow-rules-reference.md.
      5. Tests: test_scopes.py — grist.document matches the doc_id from args, not another doc_id, and an empty
         value never matches. test_catalogue.py — the grist.document catalogue entry exists with verbs read,
         create, update, restructure; rules_for_catalogue_entry("grist.document", ["DOC1"], [Verb.READ, Verb.CREATE])
         compiles to grist.read_records and grist.add_records with predicate grist.document; an empty value is
         rejected like apps_script.project's. test_gate.py — next to the apps_script.project case: a stored
         grist.document rule auto-accepts grist_get_records for that doc_id and not for another.
         test_propose.py::test_the_extra_scope_operations_stay_unproposed (l.261): add the five Grist tools with
         make_ctx(connector="grist", args={"doc_id": "DOC1", "table_id": "Table1"}) to its tuple.
         test_registry.py: add the five grist.* operation keys to _GRANT_MANIFEST_UNREACHABLE_OPERATIONS (l.31) and
         update its comment ("six" → "eleven", and name grist.document among the EXTRA_SCOPES).
      6. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if propose.proposals_for returns a proposal for a Grist tool without a PROPOSABLE_SCOPES
      entry, stop with status=blocked (plan §6).
    acceptance:
      - python3 -m pytest tests/unit/policy -q passes
      - python3 -m pytest tests/unit/test_gate.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n '"grist.document"' src/privacyfence/policy/scopes.py src/privacyfence/policy/catalogue.py matches in both files
      - grep -c "grist" src/privacyfence/policy/propose.py prints a number of at least 1, and grep -n '_scope("grist' src/privacyfence/policy/propose.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p8-local-settings
    title: Build the connector in the daemon, sign in from Settings and the CLI, and the QA sign-in step
    depends_on: [p3-connector-listing]
    complexity: M
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/settings_controller.py
      - scripts/qa_authenticate_connectors.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_qa_authenticate_connectors.py
    brief: |
      Read first: plan §3.5 (the spec) and §3.2.1; the Salesforce wiring this copies: daemon_main.py TOKEN_FILES (l.169-180),
      the Salesforce block of build_connectors (l.1466-1481), run_salesforce_oauth (l.1786-1802), the --salesforce-oauth
      flag and dispatch (l.2183-2192, 2299-2335); settings_controller.py authenticate_connector and
      _authenticate_salesforce (l.1258-1357), ALL_CONNECTORS, ORG_CONFIG_SERVICE, ORG_BUNDLE_SERVICES (l.89-122);
      scripts/qa_authenticate_connectors.py STEPS (l.74-84).
      1. daemon_main.py: TOKEN_FILES["grist"]; imports (GristClient, GristClientError, GristConnector, GristTokenProvider,
         and grist_oauth's oauth_config, load_token_file, check_server_matches, authorize_interactive under the names in
         §3.5); the build_connectors block exactly as §3.5; run_grist_oauth and --grist-oauth with the exact messages.
      2. settings_controller.py: everything in §3.5's settings_controller bullet.
      3. scripts/qa_authenticate_connectors.py: the Grist OAuthStep as §3.5, and its docstring/--only help.
      4. Tests: test_daemon_main.py — grist built when the bundle has a grist section and a matching token file exists
         (GristClient.check_connection monkeypatched); no bundle section → failures["grist"] == "no_org_config"; no token
         file → "not_authenticated"; a token for another server → "not_authenticated"; disabled → no entry;
         run_grist_oauth success, missing config and failure (authorize_interactive monkeypatched) with exit codes and
         messages; the --grist-oauth flag dispatches to it. test_settings_controller.py — authenticate_connector("grist")
         without config sets "Grist organization config isn't installed yet."; with config it runs
         grist_oauth.authorize_interactive (monkeypatched; drive _run_async synchronously the way the Salesforce tests
         do) against data_dir()/credentials/grist_token.json and refreshes connectors; failure sets the error; the
         grist row's has_org follows the bundle. test_qa_authenticate_connectors.py — the grist step and group.
         Update any assertion that pins the exact connector list or count to include grist.
      5. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: plan §6 "Tests that enumerate connectors".
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/test_qa_authenticate_connectors.py -q passes
      - 'grep -n "\"grist\": \"credentials/grist_token.json\"" src/privacyfence/daemon_main.py matches'
      - grep -n "\-\-grist-oauth" src/privacyfence/daemon_main.py scripts/qa_authenticate_connectors.py matches in both files
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p9-org-mode
    title: Org bundle grist section and the org-mode OAuth sign-in on /connect
    depends_on: [p8-local-settings]
    complexity: M
    touches:
      - scripts/build_org_bundle.py
      - src/privacyfence/web/routes_connect.py
      - docs/configuration-reference.md
      - tests/unit/test_build_org_bundle.py
      - tests/unit/web/test_routes_connect.py
    brief: |
      Read first: plan §3.6 (the spec) and §3.2.1; web/routes_connect.py (whole module, the Salesforce branches in
      particular, l.95-150 and 240-330); scripts/build_org_bundle.py's Salesforce option group (l.188-193, 505-511), the
      services tuple (l.692), _CONNECTOR_CALLBACKS (l.52-57) and the org-mode summary (l.735-750).
      1. scripts/build_org_bundle.py: the Grist argument group, validation, section writing, the services tuple and
         _CONNECTOR_CALLBACKS["grist"] = ("grist",) exactly as §3.6.
      2. docs/configuration-reference.md "Build options" table, after the Atlassian rows:
         "| `--grist-server-url URL` | `https://docs.getgrist.com` | `grist.server_url` | The Grist server whose documents people use. |",
         "| `--grist-client-id`, `--grist-client-secret` | none | `grist.client_id`, `grist.client_secret` | The OAuth app registered in Grist. Give both or neither. |",
         "| `--grist-auth-server-url URL` | the server URL | `grist.auth_server_url` | Where Grist's sign-in discovery document is served, when it is not the server itself. |"
         (tests/unit/test_docs_configuration_reference.py requires every option documented).
      3. web/routes_connect.py: every bullet in §3.6 for routes_connect.
      4. Tests: test_build_org_bundle.py — the four options write the section (trailing slash stripped; auth_server_url only
         when given); id without secret exits like Salesforce's pair; an http:// URL exits with the exact message; a
         Grist-only bundle is written; an org-mode build including Grist prints {issuer}/oauth/callback/grist.
         web/test_routes_connect.py — the grist row shows "Not set up by your organization" without the section and a
         Connect link to /oauth/start/grist with it; /oauth/start/grist redirects to the discovered authorization
         endpoint with PKCE, prompt=consent and the scopes (discover monkeypatched); /oauth/callback/grist exchanges the
         code (exchange_code monkeypatched) and saves user_dir(principal)/credentials/grist_token.json; the service-row
         count assertion (l.170) becomes 12.
      5. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/web/test_routes_connect.py tests/unit/test_docs_configuration_reference.py -q passes
      - grep -n '"grist"' src/privacyfence/web/routes_connect.py scripts/build_org_bundle.py matches in both files
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p10-qa-recorder
    title: Live check, lifecycle and recorded fixtures for Grist
    depends_on: [p6-schema-writes, p8-local-settings]
    complexity: M
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/qa_environment.yaml.example
      - tests/fixtures/live/grist/**
      - tests/unit/test_grist_client.py
      - src/privacyfence/grist_client.py
      - docs/connector-qa.md
    brief: |
      Read first: docs/connector-qa.md (whole), docs/testing-policy.md "Layer 5", scripts/qa_fixture_recorder.py
      _build_salesforce_client (l.948-956), check_confluence (l.811-865), lifecycle_confluence (l.2195-2240), RawCapture
      (l.595-628), and the `.claude/skills/steward/SKILL.md` notes on qa-record-fixture.yml.
      1. scripts/qa_fixture_recorder.py:
         - _build_grist_client(): cfg = grist_oauth.oauth_config(daemon_main.load_org_config().get("grist") or {});
           path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["grist"]); grist_oauth.check_server_matches(
           grist_oauth.load_token_file(path), cfg); return GristClient(cfg.server_url, GristTokenProvider(cfg, path)).
         - check_grist(record, manifest): cfg = manifest.get("grist") or {}; doc_id (required; missing → a failed
           CheckResult "grist.doc_id missing from qa_environment.yaml"), table_id default "QaSeed". Three CheckResults:
           list_documents (never recorded: ok when the list contains doc_id, or when the client raises GristAccessDenied,
           with note "server does not let OAuth apps list documents"); list_columns ("list_columns.json": the raw
           columns response for table_id, through RawCapture); get_records ("get_records.json": the raw records
           response, ok only when every returned row's Name contains [QATEST], refusing to record otherwise).
         - lifecycle_grist(manifest): in the table cfg.get("lifecycle_table_id", "QaLifecycle") — never QaSeed, whose
           rows check_grist requires to be [QATEST] — add one record {"Name": f"{LIFECYCLE_TAG} grist row {suffix}", "Note":
           "created by qa_fixture_recorder.py --lifecycle"}, read it back by id, update Note to "updated", read back; no delete (the
           client has none, plan §3.7) — docstring says rows accumulate and are cleaned by hand, like
           lifecycle_confluence. LifecycleResult("grist", ok, note, cleanup_ok=None).
         - Register "grist" in CONNECTOR_CHECKS, EXPECTED_FIXTURES ("list_columns.json", "get_records.json") and
           LIFECYCLE_CHECKS.
      2. tests/fixtures/qa_environment.yaml.example: a grist section (doc_id: "", table_id: QaSeed,
         lifecycle_table_id: QaLifecycle) with comments in the file's style. Update the comment above LIFECYCLE_CHECKS in the
         recorder to name Grist.
      3. docs/connector-qa.md: a Grist row in the QA accounts table (a getgrist.com account holding nothing real; the
         PrivacyFence QA app registered under Account settings → Developer, merged into the QA bundle with
         build_org_bundle.py's --grist-* options); "### Seed: Grist" (document "PrivacyFence QA [QATEST]", table QaSeed
         with Name and Note and two [QATEST] rows, an empty table QaLifecycle with the same columns, a contrast document;
         set grist.doc_id); the Manifest reference row (lifecycle: adds and updates one row in lifecycle_table_id, never
         deletes it); the sentence near l.352 that lists which connectors --lifecycle covers gains Grist;
         Grist in the "Authenticating connectors" list (qa_authenticate_connectors.py --only grist); "### Grist checks"
         in the exploratory section (sign-in, review card for get_records, popup cards for the four writes naming the
         server, the grist.document rule from Settings auto-accepts a read, list_documents' refusal message where the
         server refuses it).
      4. Commit and push this phase branch, then dispatch .github/workflows/qa-record-fixture.yml against it with input
         connector=grist (GitHub MCP actions_run_trigger, ref = this phase branch). A queued run is waiting on the
         connector-live-check concurrency group: wait, never re-dispatch. When it succeeds, git pull. Review every file
         under tests/fixtures/live/grist/ before continuing: no real e-mail, name other than the QA placeholders, team
         domain, token or non-[QATEST] content. If something leaks, extend the redaction in check_grist, push and
         dispatch again. Put the run URL in your final report.
      5. tests/unit/test_grist_client.py: TestLiveFixtureParsing replaying the two fixtures through the real parsers
         (list_columns, get_records via a faked _request), skipping with the record hint when a file is missing, exactly
         like tests/unit/test_salesforce_client.py:1316-1360. If a fixture shows a shape the parser mishandles, fix
         grist_client.py and its unit test (plan §6).
      6. tests/unit/test_qa_fixture_recorder.py: check_grist (including the GristAccessDenied path) and lifecycle_grist
         against a fake client (pattern: the existing per-connector tests there); TestFixturePresence passes.
      7. ruff check . and python3 -m pytest tests/unit -q. New text follows plan §3.0.
      Stop condition: plan §6 "The runner credential", and §6 "OAuth details not yet seen live".
    acceptance:
      - ls tests/fixtures/live/grist/ lists list_columns.json and get_records.json
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py tests/unit/test_grist_client.py -q passes, with TestLiveFixtureParsing not skipped
      - python3 -c "import sys; sys.path.insert(0,'scripts'); import qa_fixture_recorder as q; assert 'grist' in q.CONNECTOR_CHECKS and 'grist' in q.LIFECYCLE_CHECKS" exits 0
      - The qa-record-fixture.yml run for connector=grist on this phase branch concluded success (URL in the final report)
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p11-docs-adrs-retire
    title: Reference docs, changelog, the four ADRs, the live check, and retiring the plan
    depends_on: [p7-policy-scope, p9-org-mode, p10-qa-recorder]
    complexity: S
    touches:
      - docs/grist-setup*.md
      - docs/configuration-reference.md
      - docs/README.md
      - docs/approvals-and-policy.md
      - docs/connecting-a-service.md
      - docs/org-mode-setup-guide.md
      - CHANGELOG.md
      - docs/adr/0142-grist-signs-in-with-oauth-registered-in-the-organization-bundle.md
      - docs/adr/0143-the-grist-server-comes-only-from-the-organization-bundle.md
      - docs/adr/0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md
      - docs/adr/0145-the-grist-connector-only-adds.md
      - docs/adr/README.md
      - docs/grist-connector-plan.md
      - docs/grist-connector-plan-manual-steps.html
      - scripts/build_site.py
    brief: |
      Read first: docs/adr/README.md (template and rules), plan §3 and §4, docs/configuration-reference.md (l.95-110 and
      219-260), CHANGELOG.md's "## [Unreleased]" section.
      1. The setup guide grist-setup.md in docs/: check every statement against the code as it now is (error texts from
         grist_oauth.py and grist_client.py, the option names in build_org_bundle.py, the port and callback paths) and
         correct it.
      2. docs/configuration-reference.md: add grist to the connectors.<name>.enabled list (l.103), Grist to the sentence
         listing per-service guides (l.223-227) as "[Grist setup](grist-setup.md)", and `--grist-oauth` to the CLI flag
         table next to `--salesforce-oauth`. docs/approvals-and-policy.md: in the scope table with the "Apps Script
         project" row (l.386), add "| Grist document | identity | document ids | `grist.document` | the document is one
         of these |" after it.
         docs/connecting-a-service.md: add Grist to the provider lists (l.4, l.134, l.181) and the row
         "| Grist | 53685 |" to the loopback-port table (l.72-77). docs/org-mode-setup-guide.md: add the row
         "| Grist | `https://pf.acme.example.com/oauth/callback/grist` | `--grist-client-id`, `--grist-client-secret`, `--grist-server-url`, `--grist-auth-server-url` | [Grist setup](grist-setup.md) |"
         before the Telegram row of the per-connector table (l.191), and the four --grist-* options to the build-options table (l.271-272)
         matching configuration-reference.md's rows.
      3. CHANGELOG.md under "## [Unreleased]" (never a version heading): one Added line — "Grist connector: list tables
         and columns, read records after review, and add or update records and add tables and columns with approval,
         on getgrist.com or a self-hosted Grist with OAuth apps. Your organization registers one Grist OAuth app; each
         person signs in with Authenticate… in Settings or on the connections page. Nothing is deleted."
      4. Write ADRs 0142–0145 from plan §4 at the paths in touches, with the docs/adr/README.md template (Status
         "Accepted — <today's date>. Implemented.", Context, Decision, Alternatives considered, Consequences,
         Verification naming the tests that enforce each, Related). Link source files and ADRs 0019, 0070, 0072, 0077,
         0115 where relevant; never link the plan. Add the four rows to the index in docs/adr/README.md.
      5. git rm docs/grist-connector-plan.md docs/grist-connector-plan-manual-steps.html; remove "grist-connector-plan.md"
         from scripts/build_site.py CONTRIBUTOR_DOCS and its line from docs/README.md's contributor half;
         grep -rn "grist-connector-plan" . --exclude-dir=.git must find nothing.
      6. python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py leave no diff;
         ruff check . and python3 -m pytest tests/unit -q pass.
      7. Push this phase branch and dispatch .github/workflows/connector-live-check.yml (no inputs) against it with the
         GitHub MCP actions_run_trigger: it carries every phase, so it is the definition-of-done live check for
         src/privacyfence/*_client.py and connectors/**. A queued run is waiting on the connector-live-check concurrency
         group: wait, never re-dispatch. Put the run URL in your final report. If it opens or updates the
         chore/connector-live-fixture-drift PR for another connector, leave that PR alone (steward skill) and mention it.
    acceptance:
      - ls docs/adr/0142-*.md docs/adr/0143-*.md docs/adr/0144-*.md docs/adr/0145-*.md lists four files, each containing "Accepted"
      - grep -c "014[2-5]" docs/adr/README.md prints at least 4
      - test ! -e docs/grist-connector-plan.md && test ! -e docs/grist-connector-plan-manual-steps.html
      - grep -rn "grist-connector-plan" . --exclude-dir=.git finds nothing
      - grep -n "Grist connector" CHANGELOG.md matches a line below "## [Unreleased]" and above the next "## [" heading
      - The connector-live-check.yml run on this phase branch concluded success, or failed only in another connector's row (URL in the final report)
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
```
