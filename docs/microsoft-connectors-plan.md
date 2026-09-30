# Microsoft connectors (Outlook Mail, Calendar, Contacts, OneDrive, To Do) — plan

## 1. Goal

Add five Microsoft Graph connectors — `outlook_mail`, `outlook_calendar`, `outlook_contacts`,
`onedrive` and `todo` — with the same approval gate, privacy filtering, audit trail and
Always-allow semantics as their Google counterparts. They share one Entra app registration and one
sign-in. The tool surface reaches parity with the Google connectors wherever Microsoft Graph
allows it, and everything is verified on a **personal Microsoft account**. Work/school-only
features (Teams, SharePoint, free/busy, rooms, working location, admin consent, tenant pinning)
are out of scope and tracked in https://github.com/privacyfence/privacyfence/issues/828.
Tracking issue: https://github.com/privacyfence/privacyfence/issues/688.

The build order follows two decisions the maintainer made:

- **Live fixtures first.** The OAuth code, the shared Graph transport and the QA recorder come
  first. After those land, the maintainer signs the QA account in once, and a phase records real
  Graph responses on the self-hosted runner. Every client parser is then written against those
  recorded fixtures, not against Graph documentation alone.
- **One smoke test, at the end.** Phases have no manual or live development testing. The only
  automated live run after recording is one `connector-live-check.yml` dispatch in the last phase.
  The only human verification is the smoke test in `manual_after`.

## 2. Current state

- **No Microsoft code exists.** `grep -ri "graph.microsoft\|msal" src` finds nothing.
- **Atlassian is the pattern for a shared grant.** Its connectors share one token file
  (`daemon_main.TOKEN_FILES["atlassian"]`, `src/privacyfence/daemon_main.py:168-179`) that is
  written by `atlassian_oauth.save_token_file` (`src/privacyfence/atlassian_oauth.py:267`, via
  `secure_files.atomic_write_json`) and refreshed by each client with a re-read-first rotation guard
  (`src/privacyfence/jira_client.py:147-182`). The client id comes only from the organization
  config's `atlassian` section (`daemon_main.py:1455-1466`), which local mode also installs through
  Settings.
- **Loopback OAuth.** `oauth_loopback.run_browser_oauth(build, exchange, port, path, timeout,
  open_browser, redirect_host)` (`src/privacyfence/oauth_loopback.py:148`) runs Authorization
  Code + PKCE. Fixed ports are in use: Slack 53682, Salesforce 53683, Atlassian 53684.
  Salesforce uses `redirect_host="localhost"` (`salesforce_client.py:183-185`).
- **Org mode connect.** `web/routes_connect.py` handles per-principal consent through a server
  callback, `{issuer}/oauth/callback/{grant}`, not the loopback. It maps services to grants with
  `_GRANT_KEY` (`:108-109`) and stores tokens at `paths.user_dir(principal)/TOKEN_FILES[grant]`
  (`:127-128`).
- **QA recorder.** `scripts/qa_fixture_recorder.py` (2246 lines):
  - It records **raw** provider JSON (`:580-586`).
  - `CONNECTOR_CHECKS` (`:1535-1547`) and `EXPECTED_FIXTURES` (`:1561-1573`) must have the same
    keys; an import-time assertion enforces this (`:1580-1583`).
  - `redact()` (`:137-159`) replaces `owner`/`createdBy`/`lastModifiedBy` objects wholesale with
    a Salesforce-shaped `{"Id","Name"}`, and replaces an `emailAddress` value with a string. That
    would break every Graph shape, so Graph fixtures need their own pass.
  - `deidentify_structural_fields()` (`:250-289`) maps ids and URLs to placeholders.
  - `tests/unit/test_qa_fixture_recorder.py::TestFixturePresence` (`:1606-1639`) fails as soon as a
    connector is registered without its fixture files on disk.
- **Recording workflow.** `qa-record-fixture.yml` takes a free-string `connector` input, validated
  by `grep -q "^    \"${CONNECTOR}\": check_" scripts/qa_fixture_recorder.py`. It commits the
  fixture back to the dispatched branch and refuses `main` and `releases/*`. It shares
  concurrency group `connector-live-check` (`cancel-in-progress: false`) with the live check.
  Runner state is copied from `~/privacyfence/{credentials/, org/org_config.json,
  tests/fixtures/qa_environment.yaml}`.
- **Sizes of the counterparts.** `connectors/gmail.py` 1722 lines, `gmail_client.py` 1635,
  `connectors/drive.py` 1920, `drive_client.py` 2707, `connectors/calendar.py` 1018,
  `calendar_client.py` 1260, `connectors/contacts.py` 426, `contacts_client.py` 676,
  `connectors/tasks.py` 385, `tasks_client.py` 346. Their tool lists, gates, operation keys and
  verbs are in `auto_accept.TOOL_TO_GATE`/`TOOL_TO_OPERATION` (`auto_accept.py:83,175`) and
  `policy/registry.py:141` `TOOL_TO_VERB`.
- **Wiring a connector** touches many tables. They are listed in
  `docs/coding-and-testing-guidelines.md` §3. The ones that fail a unit test the moment a module
  appears in `src/privacyfence/connectors/` are:
  - `scripts/pyinstaller_common.py` (`test_pyinstaller_hidden_imports.py`)
  - `CONNECTOR_CLASSES` in `tests/unit/connectors/test_readme_manifest_alignment.py:36`
  - `TOOL_TO_GATE` and the `docs/tools-reference.md` rows, generated by
    `scripts/generate_tools_reference.py`, which needs `CONNECTOR_TITLES`/`CONNECTOR_SHORT`
    (`:48,63`)
  - `tests/unit/test_website_connector_pages.py::CONNECTORS` (`:28-40`). This one requires a
    `/connectors/<page>/` website page, a published setup guide in `build_site.CONNECTOR_GUIDES`
    (`scripts/build_site.py:193`) and a row in `README.md`'s Connectors table (`README.md:62-75`).

## 3. Design

### 3.1 Names

| Connector (module, `name`) | Client module / class / error | Recorder fixtures dir | Google counterpart |
|---|---|---|---|
| `outlook_mail` (`connectors/outlook_mail.py`, `OutlookMailConnector`) | `outlook_mail_client.py` / `OutlookMailClient` / `OutlookMailClientError` | `outlook_mail` | `gmail` |
| `outlook_calendar` (`connectors/outlook_calendar.py`, `OutlookCalendarConnector`) | `outlook_calendar_client.py` / `OutlookCalendarClient` / `OutlookCalendarClientError` | `outlook_calendar` | `calendar` |
| `outlook_contacts` (`connectors/outlook_contacts.py`, `OutlookContactsConnector`) | `outlook_contacts_client.py` / `OutlookContactsClient` / `OutlookContactsClientError` | `outlook_contacts` | `contacts` |
| `onedrive` (`connectors/onedrive.py`, `OneDriveConnector`) | `onedrive_client.py` / `OneDriveClient` / `OneDriveClientError` | `onedrive`, `onedrive_excel` | `drive` |
| `todo` (`connectors/todo.py`, `TodoConnector`) | `todo_client.py` / `TodoClient` / `TodoClientError` | `todo` | `tasks` |

- **Shared modules:**
  - `src/privacyfence/msgraph_oauth.py`: sign-in, refresh and the token file.
  - `src/privacyfence/msgraph_http.py`: the one Graph transport.
  - `src/privacyfence/msgraph_errors.py`: agent-facing not-found and permission messages.
- **Token and org config:**
  - Grant/`TOKEN_FILES` key: `"microsoft"`, file `credentials/microsoft_token.json`.
  - Org-config section: `"microsoft"`.
  - Org-mode callback: `{issuer}/oauth/callback/microsoft`.
- **Settings labels** (`settings_controller._CONNECTOR_LABEL_OVERRIDES`): `"outlook_mail": "Outlook
  Mail"`, `"outlook_calendar": "Outlook Calendar"`, `"outlook_contacts": "Outlook Contacts"`,
  `"onedrive": "OneDrive"`, `"todo": "Microsoft To Do"`. `routes_connect.SERVICE_LABELS` uses the
  same strings.
- **Tools reference** (`scripts/generate_tools_reference.py`):
  - `CONNECTOR_TITLES`: the labels above.
  - `CONNECTOR_SHORT`: `"Outlook Mail"`, `"Outlook Calendar"`, `"Outlook Contacts"`, `"OneDrive"`,
    `"To Do"`.
- **Website and README:**
  - Page: `/connectors/microsoft-365/` (`website/connectors/microsoft-365/index.html`).
  - Setup guide: `docs/microsoft-365-setup.md` (stem `microsoft-365-setup`).
  - `README.md` Connectors row label: `Microsoft Outlook, OneDrive, To Do`. All five connectors map
    to this one page, guide and row in `test_website_connector_pages.CONNECTORS`, the way the
    Google connectors share one.
- **Privacy groups** (`privacy_filter._GROUP_NAMES`, `settings_controller.PRIVACY_GROUP_LABELS` /
  `PRIVACY_CATEGORY_LABELS`, `resources/settings.yaml.example`, each with `default_policy: block`):

  | Group | Label | Categories and defaults |
  |---|---|---|
  | `outlook_mail_privacy` | "Outlook Mail" | same as `privacy`: `body` allow, `metadata` allow, `thread_history` allow, `attachments` block. Category labels copied from `privacy`'s. |
  | `onedrive_privacy` | "OneDrive" | same as `drive_privacy`: `file_content`, `file_metadata`, `file_list`, `folder_structure` allow |
  | `outlook_contacts_privacy` | "Outlook Contacts" | `notes` block, label "Contact notes (free-text)" |
  | `todo_privacy` | "Microsoft To Do" | `notes` block, label "Task notes (free-text)" |

  `outlook_calendar` has no group, like `calendar`.

### 3.2 Rules every phase follows

1. **Mirror the counterpart.** A tool's gate, layout (`gate._TOOL_LAYOUT`), verb
   (`TOOL_TO_VERB`), `read_only`, `destructive`, the shape of its `gated_call`, its preview fields,
   its privacy-filter calls and its tests all copy the Google counterpart named in its row
   (§3.7–§3.11). The exception is where the row or its notes say otherwise. Operation keys are
   `<connector>.<suffix>`, with the suffix given in the table. A tool with "—" as its counterpart
   gets everything spelled out in its row.
2. **Errors.** Each client raises only its own `<Name>ClientError`, which subclasses
   `msgraph_http.GraphError`. Each connector's `_fetch` wraps client calls in `asyncio.to_thread`
   and catches that error. It raises `msgraph_errors.unavailable_error(<connector>, exc,
   self.my_email)` when that returns non-`None`, and otherwise `RuntimeError(str(exc)) from exc`.
   This mirrors `connectors/tasks.py:323`.
3. **Constructor.** `<X>Connector.__init__(self, client: <X>Client)`. It sets `self.my_email = ""`;
   mail and OneDrive also set `self.download_mode = "local"`, `self.download_config = None` and
   `self.download_base_url = ""`, as `connectors/gmail.py:245-247` does.
4. **Every `gated_call` passes `tool=` before `gate=`**, because
   `test_readme_manifest_alignment.py` regex-parses the source. It also passes
   `my_email=self.my_email`.
5. **No live runs in phases.** No phase runs `qa_fixture_recorder.py`, dispatches
   `connector-live-check.yml`, or does anything manual. The exceptions are p05, which dispatches
   `qa-record-fixture.yml`, and p28, which dispatches `connector-live-check.yml` once for the
   whole family. §2.7's "live `--check` report" row is satisfied once, by p28's run. In `/dod`,
   report that row as "deferred to p28 by the plan".
6. **CHANGELOG.** Only p28 writes the `CHANGELOG.md` entry, because the connectors are not built
   by the daemon until p24. In `/dod`, report the row as "deferred to p28 by the plan".
7. **Generated docs are regenerated, never hand-edited.** Run
   `python3 scripts/generate_tools_reference.py` after any tool change and
   `python3 scripts/generate_always_allow_reference.py` after any policy change, and commit the
   output.
8. **No icons.** No `resources/connector_icons/*.png` for Microsoft. Only real brand assets go
   there, and without an icon the card renders cleanly.
9. **Comments carry no project history** (`test_code_no_history.py`, ADR 0056). Cite ADRs by
   number and open work by full issue URL.

### 3.3 `msgraph_oauth.py` (p01)

```python
MICROSOFT_OAUTH_PORT = 53685
MICROSOFT_REDIRECT_PATH = "/callback"
LOGIN_BASE_URL = "https://login.microsoftonline.com"
DEFAULT_TENANT = "common"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
DEFAULT_SCOPES: list[str] = [
    "offline_access", "User.Read", "Mail.ReadWrite", "MailboxSettings.ReadWrite",
    "Calendars.ReadWrite", "Contacts.ReadWrite", "Files.ReadWrite", "Tasks.ReadWrite",
]
CLIENT_KIND_PUBLIC = "public"
CLIENT_KIND_CONFIDENTIAL = "confidential"

class MicrosoftOAuthError(Exception): ...

def authority_url(tenant: str) -> str                      # f"{LOGIN_BASE_URL}/{tenant or DEFAULT_TENANT}/oauth2/v2.0"
def build_authorize_url(client_id: str, redirect_uri: str, state: str, code_challenge: str,
                        tenant: str = DEFAULT_TENANT, scopes: list[str] | None = None) -> str
def exchange_code(client_id: str, code: str, redirect_uri: str, code_verifier: str,
                  tenant: str = DEFAULT_TENANT, client_secret: str = "",
                  scopes: list[str] | None = None) -> dict[str, Any]
def refresh(client_id: str, refresh_token: str, tenant: str = DEFAULT_TENANT,
            client_secret: str = "", scopes: list[str] | None = None) -> dict[str, Any]
def fetch_account_email(access_token: str) -> str
def token_record(response: dict[str, Any], account_email: str, tenant: str, client_kind: str,
                 previous_refresh_token: str = "", now: float | None = None) -> dict[str, Any]
def load_token_file(token_file: str) -> dict[str, Any]
def save_token_file(token_file: str, record: dict[str, Any]) -> None
def authorize_interactive(client_id: str, token_file: str, tenant: str = DEFAULT_TENANT,
                          scopes: list[str] | None = None, port: int = MICROSOFT_OAUTH_PORT,
                          open_browser: Callable[[str], bool] | None = None) -> dict[str, Any]
```

- **`build_authorize_url`** sends these query parameters: `client_id`, `response_type=code`,
  `redirect_uri`, `response_mode=query`, `scope=" ".join(scopes or DEFAULT_SCOPES)`, `state`,
  `code_challenge`, `code_challenge_method=S256`, `prompt=select_account`.
- **`exchange_code` / `refresh`** make a form-encoded `requests.post` to
  `authority_url(tenant) + "/token"` with `timeout=30`. `client_secret` is included **only when it
  is non-empty**; Entra rejects a secret on a public-client redemption (AADSTS700025). A non-2xx
  response raises `MicrosoftOAuthError(f"Microsoft token endpoint returned {status}:
  {error_description or error}")`. The token or code is never included in the message.
- **`fetch_account_email`** makes a GET to `GRAPH_ME_URL` with `params={"$select":
  "mail,userPrincipalName"}` and a bearer header. It returns `mail or userPrincipalName or ""`, and
  returns `""` on any exception after logging a warning.
- **`token_record`** returns `{"access_token", "refresh_token": response.get("refresh_token") or
  previous_refresh_token, "expires_at": now + int(response.get("expires_in", 3600)), "scope":
  response.get("scope", ""), "account_email", "tenant", "client_kind"}`.
- **`load_token_file`**: a missing file, or a file without `refresh_token`, raises
  `MicrosoftOAuthError("Microsoft is not authenticated. Use Authenticate… in PrivacyFence Settings
  to sign in.")`. The wording matters: `_classify_connector_failure` keys on "Use Authenticate…".
- **`save_token_file`** is `secure_files.atomic_write_json(token_file, record)`.
- **`authorize_interactive`** is a public client: no secret, and
  `run_browser_oauth(..., port=port, path=MICROSOFT_REDIRECT_PATH, redirect_host="localhost",
  open_browser=open_browser)`. The redirect URI is therefore
  `http://localhost:53685/callback`. It then calls `fetch_account_email`, saves
  `token_record(..., client_kind=CLIENT_KIND_PUBLIC)`, and returns the record.
  `OAuthLoopbackError` is wrapped in `MicrosoftOAuthError`.

**Organization config section** (`org_config["microsoft"]`): `{"client_id": str, "tenant": str
(optional, default "common"), "client_secret": str (optional; required only for org-mode web
sign-in)}`.

`scripts/build_org_bundle.py` gets these changes:

- An argument group "Microsoft (Outlook, OneDrive, To Do)" with `--microsoft-client-id`,
  `--microsoft-client-secret` and `--microsoft-tenant` (default `common`).
- `bundle["microsoft"] = {"client_id": ..., "tenant": ...}`, plus `"client_secret"` only when one
  is given.
- `_CONNECTOR_CALLBACKS["microsoft"] = ("microsoft",)`, and `"microsoft"` appended to the services
  tuple at `:692`.
- `--microsoft-client-secret` without `--microsoft-client-id` is a usage error.

`daemon_main.py` gets these changes:

- `TOKEN_FILES["microsoft"] = "credentials/microsoft_token.json"`.
- `run_microsoft_oauth(org_config) -> int`. It prints "No Microsoft organization config
  installed." to stderr and returns 1 when `client_id` is missing. Otherwise it calls
  `msgraph_oauth.authorize_interactive(client_id, _resolve_path(TOKEN_FILES["microsoft"]),
  tenant=section.get("tenant") or DEFAULT_TENANT)`, prints `Signed in as <email>`, and returns 0.
  On `MicrosoftOAuthError` it prints the message and returns 1. The flag is `--microsoft-oauth`,
  wired exactly like `--atlassian-oauth` (`daemon_main.py:2071, 2179-2184, 2212-2213`).

`scripts/qa_authenticate_connectors.py` gets `OAuthStep("microsoft", "microsoft",
"--microsoft-oauth", "Microsoft (Outlook, OneDrive, To Do)")`, appended last in `STEPS`.

### 3.4 `msgraph_http.py` and `msgraph_errors.py` (p02)

```python
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
REQUEST_TIMEOUT_SECONDS = 30
MAX_THROTTLE_RETRIES = 3
DEFAULT_RETRY_AFTER_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 30.0
REFRESH_MARGIN_SECONDS = 120
UPLOAD_CHUNK_BYTES = 5 * 1024 * 1024          # a multiple of 320 KiB, as upload sessions require
SIMPLE_UPLOAD_MAX_BYTES = 4 * 1024 * 1024
_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()

class GraphError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = "") -> None  # sets .status, .code

class GraphHttp:
    def __init__(self, org_section: dict[str, Any], token_file: str,
                 error_cls: type[GraphError] = GraphError,
                 session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None
    account_email: str                                   # property, from the token record
    def request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                json_body: Any = None, data: bytes | None = None,
                headers: dict[str, str] | None = None) -> Any
    def get(self, path: str, params: dict[str, str] | None = None) -> Any
    def post(self, path: str, json_body: Any = None) -> Any
    def patch(self, path: str, json_body: Any) -> Any
    def delete(self, path: str) -> None
    def list_all(self, path: str, params: dict[str, str] | None = None, max_items: int = 100) -> list[dict[str, Any]]
    def get_bytes(self, path: str, max_bytes: int) -> bytes
    def put_bytes(self, path: str, data: bytes, content_type: str) -> Any
    def upload_large(self, create_session_path: str, create_body: dict[str, Any], data: bytes) -> Any
    def check_connection(self) -> str                    # GET /me; returns account email
```

- **Paths.** `path` is either relative (`"/me/messages"`, joined to `GRAPH_BASE_URL`) or an
  absolute URL that must start with `GRAPH_BASE_URL + "/"`. Anything else raises
  `error_cls("Refusing to send the Microsoft token to <host>", 0, "bad_url")`. **The bearer token
  never goes to any other host.**
- **`request` is the single choke point.** It returns parsed JSON, or `None` for 204 and empty
  bodies. In order, it:
  1. refreshes the token when `expires_at - REFRESH_MARGIN_SECONDS <= clock()`;
  2. sends with `Authorization: Bearer`;
  3. on the first 401, forces a refresh and retries once;
  4. on 429/503/504, sleeps `min(float(Retry-After or DEFAULT_RETRY_AFTER_SECONDS),
     MAX_RETRY_AFTER_SECONDS)` and retries, up to `MAX_THROTTLE_RETRIES`;
  5. on any other status ≥ 400, raises `error_cls(f"Microsoft Graph error {status} ({code}):
     {message}", status, code)` from the `{"error": {"code", "message"}}` body (`code=""`,
     `message=reason` when the body isn't JSON);
  6. on `requests.RequestException`, raises `error_cls(f"Microsoft Graph request failed:
     {type(exc).__name__}", 0, "network")`.
- **Refresh.** Refresh takes `_REFRESH_LOCKS[token_file]`. Inside the lock it:
  1. re-reads the token file, and adopts the file's record when its `access_token` differs from
     the in-memory one and its `expires_at` is still outside the margin (another client already
     refreshed);
  2. otherwise calls `msgraph_oauth.refresh(client_id, refresh_token, tenant,
     client_secret=org_section.get("client_secret", "") if record["client_kind"] ==
     "confidential" else "")`;
  3. saves `token_record(..., previous_refresh_token=old)` with `save_token_file` and adopts it.

  Two error cases:
  - `MicrosoftOAuthError` from `load_token_file` in `__init__` becomes `error_cls(str(exc), 401,
    "not_authenticated")`.
  - A refresh failure becomes `error_cls("Microsoft sign-in expired or was revoked. Use
    Authenticate… in PrivacyFence Settings to sign in again.", 401, "invalid_grant")`.
- **`list_all`** follows `@odata.nextLink` (through `request`, so the host check applies). It
  stops at `max_items` and returns the concatenated `value` lists.
- **`get_bytes`** streams the response through `requests` (`stream=True`), which follows the 302
  to the pre-authenticated download URL; `requests` strips `Authorization` on a cross-host
  redirect. Once more than `max_bytes` have been read it raises `error_cls(f"File is larger than
  {max_bytes} bytes", 413, "too_large")`.
- **`upload_large`** creates an upload session by POSTing `create_body` to `create_session_path`,
  then PUTs `UPLOAD_CHUNK_BYTES` slices to the returned `uploadUrl`. Those PUTs carry `Content-Range`
  and **no** Authorization header, and go through a plain `session.put`, since the upload URL is
  pre-authenticated and on another host. The `uploadUrl` must be `https`. It returns the final
  response's JSON (the created item).
- **`_REFRESH_LOCKS` is module-level state.** Add `msgraph_http._REFRESH_LOCKS.clear()` to
  `tests/conftest.py::_reset()`.

`msgraph_errors.py` mirrors `google_errors.py:109-175`:

```python
class GraphResourceUnavailableError(RuntimeError): ...
NOT_FOUND_CODES = frozenset({"ErrorItemNotFound", "itemNotFound", "ResourceNotFound", "ErrorInvalidIdMalformed"})
FORBIDDEN_CODES = frozenset({"ErrorAccessDenied", "accessDenied", "Authorization_RequestDenied"})
def unavailable_error(connector: str, exc: BaseException, account: str) -> GraphResourceUnavailableError | None
```

The kind is "not found" when `status == 404 or code in NOT_FOUND_CODES`, and "no permission" when
`status == 403 and code in FORBIDDEN_CODES`; otherwise the function returns `None`. `who` is `the
connected Microsoft account (<account>)` when `account` looks like an address, otherwise `the
connected Microsoft account`. The templates are (not-found, no-permission):

- `outlook_mail`: "Outlook says this message, conversation, folder, category, rule or attachment
  does not exist in the mailbox of {who}. It may have been deleted, or the ID may belong to a
  different account." / "Outlook says {who} does not have permission for this item."
- `outlook_calendar`: "Outlook Calendar says this event or calendar does not exist for {who}. It
  may have been deleted, or the ID may belong to a different account." / "Outlook Calendar says
  {who} does not have permission for this event or calendar."
- `outlook_contacts`: "Outlook says this contact does not exist for {who}. It may have been
  deleted, or the ID may belong to a different account." / "Outlook says {who} does not have
  permission for this contact."
- `onedrive`: "OneDrive says this file or folder does not exist or is not shared with {who}. Check
  the file ID." / "OneDrive says {who} does not have permission for this file or folder."
- `todo`: "Microsoft To Do says this task or list does not exist for {who}. It may have been
  deleted, or the ID may belong to a different account." / "Microsoft To Do says {who} does not
  have permission for this task or list."

### 3.5 Recorder (p04, p05)

The Microsoft checks call `GraphHttp` directly, so the fixtures are raw Graph JSON and can be
recorded before any client exists (ADR 0119). Build the transport with:

```python
def _build_msgraph_http() -> GraphHttp:
    org_config = daemon_main.load_org_config()
    section = org_config.get("microsoft") or {}
    if not section.get("client_id"):
        raise SystemExit("Microsoft organization config not installed -- see docs/connector-qa.md")
    return GraphHttp(section, daemon_main._resolve_path(daemon_main.TOKEN_FILES["microsoft"]))
```

**`redact_msgraph(value)`** runs *instead of* `redact()` for every Microsoft fixture, and then
`deidentify_structural_fields()` runs on its output. It deep-copies the value, then walks it
recursively and applies these rules:

- **`emailAddress`.** Any dict value under the key `emailAddress` becomes
  `{"name": "QA Placeholder", "address": "qa-placeholder@example.com"}`. This covers `from`,
  `sender`, `toRecipients`, `ccRecipients`, `replyTo`, `organizer` and `attendees`.
- **Identity sets.** Any dict value under `createdBy`, `lastModifiedBy`, `owner`, `sharedBy`,
  `createdByUser` or `lastModifiedByUser` has each of its sub-dicts `user`, `application`,
  `device` and `group` replaced with `{"displayName": "QA Placeholder", "id":
  "qa-placeholder-account-id"}`. When the original sub-dict had an `email` key, the replacement
  also gets `"email": "qa-placeholder@example.com"`.
- **`@odata` keys.**
  - `@odata.context` becomes `"https://graph.microsoft.com/v1.0/$metadata#qa-placeholder"`.
  - `@odata.etag` becomes `"W/\"qa-placeholder\""`.
  - `@odata.nextLink` and `@odata.deltaLink` become `"https://example.com/qa-placeholder"`.
- **Ids.** A **string** value whose key, lower-cased, ends with `id`, or is one of `changekey`,
  `conversationindex`, `etag`, `ctag` or `@odata.id`, becomes a stable `qa-placeholder-id-N`. It
  shares one `_id_map` with the later `deidentify_structural_fields` call, and a value that is
  already a placeholder is skipped. This covers `conversationId`, `parentFolderId`,
  `internetMessageId`, `iCalUId`, `seriesMasterId`, `driveId`, `transactionId`, `uid` and `id`.
- **URLs.** URL keys (`webLink`, `webUrl`, `@microsoft.graph.downloadUrl`, `onlineMeetingUrl`)
  are left to `deidentify_structural_fields`, which blanks every key containing `url` and the
  keys in `_STRUCTURAL_URL_KEYS`.

`_keep_tagged(raw: dict, predicate: Callable[[dict], bool]) -> dict` returns a copy of `raw` with
`value` filtered to the items matching `predicate`. **A list fixture never records an untagged
item.**

Manifest sections, added to `tests/fixtures/qa_environment.yaml.example`. The code uses these
values as defaults, so blank or missing keys resolve by name:

```yaml
outlook_mail:
  seed_subject: "PrivacyFence QA seed message [QATEST]"
  seed_message_id: ""
  folder_name: "PrivacyFence QA Folder [QATEST]"
  category_name: "PrivacyFence QA Category [QATEST]"
  rule_name: "PrivacyFence QA rule [QATEST]"
outlook_calendar:
  calendar_name: "PrivacyFence test [PFQA]"
  seed_event_subject: "PrivacyFence QA seed event [QATEST]"
  window_start: "2030-01-01T00:00:00Z"
  window_end: "2030-02-01T00:00:00Z"
outlook_contacts:
  seed_contact_display_name: "PrivacyFence QA Test Contact [QATEST]"
onedrive:
  folder_name: "PrivacyFence QA Sandbox"
  seed_file_name: "PrivacyFence QA seed note [QATEST].txt"
onedrive_excel:
  folder_name: "PrivacyFence QA Sandbox"
  seed_workbook_name: "PrivacyFence QA seed workbook [QATEST].xlsx"
  sheet_name: "QA"
  range: "A1:B2"
todo:
  list_name: "PrivacyFence QA List"
  contrast_list_name: "PrivacyFence QA Contrast List"
  seed_task_title: "PrivacyFence QA seed task [QATEST]"
```

Checks and fixtures. Each row is one `CheckResult`. A resolve step failing means one failed result
with a clear note and no further calls.

| Check function | Fixture file | Graph call | Guardrail (must hold to record) |
|---|---|---|---|
| `check_outlook_mail` | — (resolve) | `GET /me/mailFolders/inbox/messages` `{"$filter": "subject eq '<seed_subject>'", "$top": "5"}`, unless `seed_message_id` is set | ≥ 1 result |
| | `get_message.json` | `GET /me/messages/{id}` | `subject` contains `[QATEST]` |
| | `list_conversation_messages.json` | `GET /me/messages` `{"$filter": "conversationId eq '<conversationId>'", "$top": "25"}`, then `_keep_tagged` on `subject` | ≥ 2 items kept |
| | `list_attachments.json` | `GET /me/messages/{id}/attachments` | ≥ 1 item |
| | `list_folders.json` | `GET /me/mailFolders` `{"$top": "100"}`, keep `displayName == folder_name` | exactly 1 kept |
| | `list_categories.json` | `GET /me/outlook/masterCategories`, keep `displayName == category_name` | exactly 1 kept |
| | `list_rules.json` | `GET /me/mailFolders/inbox/messageRules`, keep `displayName == rule_name` | exactly 1 kept |
| `check_outlook_calendar` | `list_calendars.json` | `GET /me/calendars`, keep `name == calendar_name` | exactly 1 kept |
| | `list_events.json` | `GET /me/calendars/{cal}/calendarView` `{"startDateTime": window_start, "endDateTime": window_end}`, keep `subject` containing `[QATEST]` | ≥ 1 kept |
| | `get_event.json` | `GET /me/calendars/{cal}/events/{first kept id}` | `subject` contains `[QATEST]` |
| `check_outlook_contacts` | `list_contacts.json` | `GET /me/contacts` `{"$filter": "displayName eq '<name>'"}`, keep tagged `displayName` | ≥ 1 kept |
| | `get_contact.json` | `GET /me/contacts/{id}` | `displayName` contains `[QATEST]` |
| `check_onedrive` | `get_folder.json` | `GET /me/drive/root:/<urllib.parse.quote(folder_name)>` | `name == folder_name` and `folder` key present |
| | `list_folder.json` | `GET /me/drive/items/{folder id}/children`, keep `name` containing `[QATEST]` | ≥ 1 kept |
| | `get_file_metadata.json` | `GET /me/drive/items/{id of seed_file_name}` | `name == seed_file_name` |
| `check_onedrive_excel` | `list_worksheets.json` | `GET /me/drive/items/{workbook id}/workbook/worksheets`. The workbook id is resolved from the folder's children by `name == seed_workbook_name`. | a worksheet named `sheet_name` |
| | `get_range.json` | `GET /me/drive/items/{wb}/workbook/worksheets/{quote(sheet_name)}/range(address='{range}')` | some cell in `values` contains `[QATEST]` |
| `check_todo` | `list_task_lists.json` | `GET /me/todo/lists`, keep `displayName in {list_name, contrast_list_name}` | both kept |
| | `list_tasks.json` | `GET /me/todo/lists/{QA list id}/tasks`, keep `title` containing `[QATEST]` | ≥ 1 kept |
| | `get_task.json` | `GET /me/todo/lists/{id}/tasks/{first kept id}` | `title` contains `[QATEST]` |

When recording, `raw = deidentify_structural_fields(redact_msgraph(value))`. The live
`TestLiveFixtureParsing` classes (p06–p12) parse these files with the clients' `_parse_*`
methods.

### 3.6 Family wiring (p03, p24)

- **`settings_controller.py`** (p03):
  - `MICROSOFT_CONNECTORS: set[str] = set()`, which p24 fills with the five names.
  - `ORG_BUNDLE_SERVICES` gains `"microsoft"`.
  - `authenticate_connector` gains `elif connector in MICROSOFT_CONNECTORS:
    self._authenticate_microsoft(org_config)`.
  - `_authenticate_microsoft` mirrors `_authenticate_atlassian` (`:1297-1344`), with three
    differences: it marks every name in `MICROSOFT_CONNECTORS` busy, it calls
    `msgraph_oauth.authorize_interactive(client_id, str(data_dir() / TOKEN_FILES["microsoft"]),
    tenant=section.get("tenant") or "common")`, and it uses these errors: "Microsoft organization
    config isn't installed yet." and `f"Microsoft authentication failed: {result}"`.
- **`web/routes_connect.py`** (p03):
  - `MICROSOFT_SERVICES: frozenset[str] = frozenset()` (filled in p24) joins `OAUTH_SERVICES`.
  - `_GRANT_KEY[s] = "microsoft"`, `_ORG_CONFIG_SECTION[s] = "microsoft"`.
  - `_is_configured` for Microsoft requires `client_id` **and** `client_secret`, because org
    mode's web callback is a confidential client.
  - The authorize URL uses `msgraph_oauth.build_authorize_url(client_id, redirect_uri, state,
    challenge, tenant)` with `redirect_uri = f"{base_url}/oauth/callback/microsoft"`.
  - The exchange uses `msgraph_oauth.exchange_code(..., client_secret=section["client_secret"])`,
    then saves `token_record(..., client_kind=CLIENT_KIND_CONFIDENTIAL)` after
    `fetch_account_email`.
  - Page rows render for `MICROSOFT_SERVICES` beside the Atlassian rows.
  - Tests monkeypatch `MICROSOFT_SERVICES` to `frozenset({"todo"})` until p24.
- **`daemon_main.build_connectors`** (p24):
  - Load the family once, as `:1455-1466` does for Atlassian: `microsoft_org =
    org_config.get("microsoft") or {}`; when it has a `client_id`, `microsoft_token =
    load_msgraph_token(...)`, or `None` on `MicrosoftOAuthError`.
  - One block per connector, in the order `outlook_mail`, `outlook_calendar`, `outlook_contacts`,
    `onedrive`, `todo`, each honoring `enabled(name)`:
    1. No org section → raise `<X>ClientError("Microsoft organization config not installed")`.
    2. No token → raise `<X>ClientError("<Label> is not authenticated. Use Authenticate… in
       PrivacyFence Settings.")`.
    3. `client = <X>Client(microsoft_org, _resolve_path(TOKEN_FILES["microsoft"]))`, then
       `client.check_connection()`.
    4. `connector = <X>Connector(client)`, `connector.my_email = microsoft_token.get("account_email",
       "")`.
    5. Mail and OneDrive also set `download_mode`/`download_config`/`download_base_url` exactly as
       the Gmail and Drive blocks do (`:1279-1312`).
    6. Failures go to `failures[name] = _classify_connector_failure(exc)`.
  - Every client constructor is `(<org_section>, token_file)` and builds `GraphHttp(org_section,
    token_file, error_cls=<X>ClientError)`. `check_connection()` returns `self._http.check_connection()`.
- **`settings_controller`** (p24): `ALL_CONNECTORS` gains the five names after `"telegram"`, and
  `_CONNECTOR_LABEL_OVERRIDES` gets the §3.1 labels. `ORG_CONFIG_SERVICE[name] = "microsoft"`,
  and `MICROSOFT_CONNECTORS` gets the five names. **`routes_connect`** (p24): `MICROSOFT_SERVICES`
  gets the five names, and `SERVICE_LABELS` the §3.1 labels.

### 3.7 To Do (`todo_client.py` p06, `connectors/todo.py` p13–p14)

**Client.**

- **Dataclasses.**
  - `TodoList(id, name, is_default: bool = False)`, from `displayName` and `wellknownListName ==
    "defaultList"`.
  - `TodoTask(id, list_id, title, notes = "", status = "", importance = "", due = "", completed =
    "", created = "", updated = "", has_attachments = False, checklist_count = 0)`.
  - `due` and `completed` are `YYYY-MM-DD`, from `dueDateTime.dateTime[:10]` and
    `completedDateTime.dateTime[:10]`. `notes` comes from `body.content`.
  - Static parsers: `_parse_list(raw)` and `_parse_task(raw, list_id)`.
- **Methods.** Due dates accept `YYYY-MM-DD` or RFC 3339; only the first 10 characters are used.
  - `list_task_lists()`: `list_all("/me/todo/lists")`.
  - `list_tasks(list_id, show_completed=False, max_items=100)`: adds `{"$filter": "status ne
    'completed'"}` unless `show_completed` is true.
  - `get_task(list_id, task_id)`: `{"$expand": "checklistItems"}`, with `checklist_count =
    len(checklistItems)`.
  - `create_task(list_id, title, notes="", due="")`: POST `{"title"}`, plus `"body": {"content":
    notes, "contentType": "text"}` when there are notes, plus `"dueDateTime": {"dateTime":
    f"{due[:10]}T00:00:00.0000000", "timeZone": "UTC"}` when there is a due date.
  - `update_task(list_id, task_id, title="", notes="", due="")`: PATCH only the non-empty fields
    (blank means unchanged, as in `tasks_update_task`).
  - `complete_task`: PATCH `{"status": "completed"}`. `uncomplete_task`: PATCH `{"status":
    "notStarted"}`.
  - `delete_task`: DELETE.
  - `move_task(source_list_id, task_id, destination_list_id)`:
    1. `get_task`.
    2. When `has_attachments` or `checklist_count > 0`, raise `TodoClientError("Microsoft To Do
       has no move; PrivacyFence re-creates the task in the destination list and deletes the
       original, which would lose this task's attachments or checklist steps. Move it in To Do
       instead.")`.
    3. Otherwise create it in the destination with `title`, `notes`, `due`, `importance` and
       `status`, delete the original, and return the new task (ADR 0118).

**Tools.** Every tool has the required `reason` param.

| Tool | Counterpart | Gate | Op suffix | Params (beyond the counterpart's, or replacing them) |
|---|---|---|---|---|
| `todo_list_task_lists` | `tasks_list_task_lists` | auto | — | — |
| `todo_list_tasks` | `tasks_list_tasks` | auto | — | same |
| `todo_get_task` | `tasks_get_task` | auto | — | same |
| `todo_create_task` | `tasks_create_task` | popup | `create_task` | `due` is `YYYY-MM-DD` |
| `todo_update_task` | `tasks_update_task` | popup | `update_task` | same |
| `todo_complete_task` | `tasks_complete_task` | popup | `complete_task` | same |
| `todo_uncomplete_task` | `tasks_uncomplete_task` | popup | `uncomplete_task` | same |
| `todo_move_task` | `tasks_move_task` | popup | `move_task` | same. The description says the task is re-created and gets a new id. The effect sentence: "The task is re-created in the destination list with a new ID, and the original is deleted." |

Task notes go through `apply_text("todo_privacy", "notes", ...)` on reads, as `tasks.py:384`
does.

### 3.8 Outlook Contacts (`outlook_contacts_client.py` p07, `connectors/outlook_contacts.py` p15)

**Client.**

- **Dataclass.** `OutlookContact(id, display_name, given_name = "", surname = "", emails:
  list[str], phones: list[dict[str, str]], company_name = "", job_title = "", notes = "",
  categories: list[str], created = "", updated = "")`.
  - `phones` items are `{"type": "mobile"|"home"|"business", "value": ...}`.
  - `notes` comes from `personalNotes`.
  - Static parser: `_parse_contact(raw)`.
- **Methods.**
  - `list_contacts(max_results=50)`: `{"$top": str(max_results), "$orderby": "displayName"}`.
  - `search_contacts(query, max_results=20)`: `$filter`
    `startswith(displayName,'{q}') or startswith(givenName,'{q}') or startswith(surname,'{q}') or
    emailAddresses/any(a:startswith(a/address,'{q}'))`, where `q = query.replace("'", "''")`.
  - `get_contact(contact_id)`.
  - `create_contact(fields)` and `update_contact(contact_id, fields)` map the counterpart's
    fields to Graph:
    - `display_name` → `displayName`
    - `emails` (the counterpart's JSON list of `{"value", "type"}`, parsed with
      `connectors/contacts.py`'s `_parse_json_list`) → `emailAddresses: [{"address": value,
      "name": display_name}]`
    - `phones`: `type == "mobile"` → `mobilePhone` (the first one), `"home"` → `homePhones`,
      anything else → `businessPhones`
    - `organization` → `companyName`, `job_title` → `jobTitle`, `notes` → `personalNotes`
  - `set_categories(contact_id, categories)`: PATCH `{"categories": [...]}`.

**Tools.**

| Tool | Counterpart | Gate | Op suffix | Notes |
|---|---|---|---|---|
| `outlook_contacts_list` | `contacts_list` | auto | — | no `source` param |
| `outlook_contacts_search` | `contacts_search` | auto | — | no `source` param |
| `outlook_contacts_get` | `contacts_get` | auto | — | `contact_id` replaces `resource_name`; no `source` |
| `outlook_contacts_update` | `contacts_update` | popup | `edit` | `contact_id` replaces `resource_name`. The other params are the same, and `args=` passes `emails`/`phones` so the `no_contact_info_change` condition applies. |
| `outlook_contacts_create` | `contacts_create` | popup | `create` | same |
| `outlook_contacts_add_category` | `contacts_add_label` | popup | `add_category` | `contact_id`, `category_name` |
| `outlook_contacts_remove_category` | `contacts_remove_label` | popup | `remove_category` | `contact_id`, `category_name` |

Notes go through `apply_text("outlook_contacts_privacy", "notes", ...)`.

### 3.9 Outlook Calendar (`outlook_calendar_client.py` p08, `connectors/outlook_calendar.py` p16–p17)

**Client.**

- **Dataclasses.**
  - `OutlookCalendar(id, name, is_default: bool, can_edit: bool, owner_email: str)`.
  - `OutlookEvent(id, calendar_id, subject, start, end, time_zone, is_all_day, location, body,
    organizer_email, attendees: list[str], sensitivity, show_as, categories: list[str], web_link,
    is_organizer: bool)`.
  - `start` and `end` are the `dateTime` strings; `time_zone` is `start.timeZone`. `body` is
    `html_to_text(body.content)` when `contentType == "html"`.
  - `OutlookCategory(name, color)`.
  - Static parsers: `_parse_calendar`, `_parse_event(raw, calendar_id)`, `_parse_category`.
- **Methods.** A blank `calendar_id` means the default calendar, i.e. paths under
  `/me/calendar/...`.
  - `list_calendars()`.
  - `list_events(calendar_id, time_min, time_max, max_results)`: `calendarView` with
    `startDateTime`/`endDateTime`, `$top`, and `$orderby=start/dateTime`.
  - `get_event(calendar_id, event_id)`.
  - `create_event(calendar_id, subject, start, end, time_zone="UTC", location="", body="",
    attendees: list[str] = [], is_all_day=False)`.
  - `update_event(calendar_id, event_id, **changed)`: PATCH only the given fields.
  - `delete_event`.
  - `set_sensitivity(calendar_id, event_id, "normal"|"private")`.
  - `set_categories(calendar_id, event_id, categories)`.
  - `list_categories()`: `GET /me/outlook/masterCategories`.
  - `create_out_of_office(start, end, subject="Out of office")`: POSTs an event to the default
    calendar with `showAs: "oof"`, `isAllDay: true` when both values are dates, and
    `sensitivity: "normal"`.

**Tools.**

| Tool | Counterpart | Gate | Op suffix | Notes |
|---|---|---|---|---|
| `outlook_calendar_list_calendars` | `calendar_list_calendars` | auto | — | |
| `outlook_calendar_list_events` | `calendar_list_events` | auto | — | params `calendar_id=""`, `time_min=""` (ISO; default now), `time_max=""` (default `time_min` + 7 days), `max_results=50` |
| `outlook_calendar_get_event_details` | `calendar_get_event_details` | review | `read_event_details` | params `calendar_id`, `event_id` |
| `outlook_calendar_get_event_visibility` | `calendar_get_event_visibility` | auto | — | returns `normal` or `private` |
| `outlook_calendar_list_categories` | `calendar_list_colors` | auto | — | |
| `outlook_calendar_create_event` | `calendar_create_event` | popup | `create_modify_event` | params `calendar_id`, `subject`, `start`, `end`, `time_zone="UTC"`, `location=""`, `description=""`, `attendees=""` (comma-separated), `all_day=False`. Effect: "The event is added to that calendar, and Outlook emails an invitation to every attendee straight away." |
| `outlook_calendar_update_event` | `calendar_update_event` | popup | `create_modify_event` | the same optional fields plus `event_id`. Effect: "The event changes, and Outlook emails an update to its attendees." |
| `outlook_calendar_delete_event` | `calendar_delete_event` | popup | `delete_event` | `destructive=True`; add to `DESTRUCTIVE_TOOLS` in `tests/unit/test_connector_tool_annotations.py` |
| `outlook_calendar_create_out_of_office` | `calendar_create_out_of_office` | popup | `out_of_office` | params `start`, `end`, `subject="Out of office"`. Acts on the default calendar and takes no `calendar_id`. Effect: "An all-day 'Out of office' event is added to your default calendar. Automatic email replies are not changed." |
| `outlook_calendar_set_event_visibility` | `calendar_set_event_visibility` | popup | `set_visibility` | `visibility` is `normal` or `private`; any other value is rejected before the gate |
| `outlook_calendar_set_event_category` | `calendar_set_event_color` | popup | `set_category` | `category_name`; `""` clears the categories |

p16 does list_calendars, list_events, get_event_details, create, update and delete. p17 does
get_event_visibility, list_categories, create_out_of_office, set_event_visibility and
set_event_category.

### 3.10 OneDrive (`onedrive_client.py` p09–p10, `connectors/onedrive.py` p18–p20)

**Client — files (p09).**

- **Dataclass.** `OneDriveItem(id, name, is_folder, size, mime_type, parent_id, parent_path,
  created, modified, web_url, created_by, modified_by)`. Static parser: `_parse_item(raw)`.
- **Methods.**
  - `list_files(query="", max_results=25)`: `/me/drive/root/children` when the query is blank,
    otherwise `/me/drive/root/search(q='{q}')` with `'` doubled.
  - `list_folder(folder_id="root", max_results=100)`: `/me/drive/items/{id}/children`, with
    `root` → `/me/drive/root/children`.
  - `get_item(item_id)`.
  - `download(item_id, max_bytes)`: `get_bytes(f"/me/drive/items/{id}/content", max_bytes)`.
  - `upload(parent_id, name, data, conflict="fail")`:
    - `len(data) <= SIMPLE_UPLOAD_MAX_BYTES`: `put_bytes(f"/me/drive/items/{parent}:/{quote(name)}:/content?@microsoft.graph.conflictBehavior={conflict}",
      ...)`.
    - Otherwise `upload_large(f"/me/drive/items/{parent}:/{quote(name)}:/createUploadSession",
      {"item": {"@microsoft.graph.conflictBehavior": conflict}}, data)`.
  - `move(item_id, destination_folder_id, new_name="")`: PATCH `{"parentReference": {"id": dest}}`,
    plus `"name"` when one is given.
  - `write_content(item_id, data)`: `put_bytes(f"/me/drive/items/{id}/content", ...)`.

**Client — Excel (p10).** Every method takes `item_id`, and `sheet` names are URL-quoted.

- **Dataclass.** `OneDriveWorksheet(id, name, position, visibility)`. Static parser:
  `_parse_worksheet(raw)`. `_parse_range_values(raw) -> list[list[Any]]` returns `raw["values"]`.
- **Methods.**
  - `list_worksheets(item_id)`: `/workbook/worksheets`.
  - `get_range(item_id, sheet, address)`: `/workbook/worksheets/{sheet}/range(address='{address}')`.
  - `write_range(item_id, sheet, address, values)`: PATCH the same path with `{"values": values}`.
  - `add_worksheet(item_id, name)`: POST `/workbook/worksheets/add` `{"name"}`.
  - `rename_worksheet(item_id, sheet, new_name)`: PATCH `/workbook/worksheets/{sheet}` `{"name"}`.
  - `format_range(item_id, sheet, address, bold=None, italic=None, font_color="", fill_color="",
    number_format="")`: PATCH `.../range(address=..)/format/font` for `bold`, `italic` and
    `color`; PATCH `.../format/fill` for `color`; PATCH the range `{"numberFormat": [[fmt]]}`.
  - `insert_range(item_id, sheet, address, shift)`: POST `.../range(address=..)/insert`
    `{"shift": "Down"|"Right"}`.
  - `delete_range(item_id, sheet, address, shift)`: POST `.../delete` `{"shift": "Up"|"Left"}`.

**Tools.** The file-source params (`local_path` | `content_base64` | `upload_id`) and their
handling copy `drive_upload_file` exactly (`connectors/drive.py:1290-1460`, including ADR 0102's
slot consumption after the gate).

| Tool | Counterpart | Gate | Op suffix | Notes |
|---|---|---|---|---|
| `onedrive_list_files` | `drive_list_files` | auto | — | params `query=""`, `max_results=25` |
| `onedrive_list_folder` | `drive_list_folder` | auto | — | `folder_id="root"` |
| `onedrive_get_file_metadata` | `drive_get_file_metadata` | auto | — | `file_id` |
| `onedrive_get_file_content` | `drive_get_file_content` | review | `read_file_contents` | text via `text_extraction.extract_text(bytes, mime)`, with the counterpart's size cap |
| `onedrive_download_file` | `drive_download_file` | review | `download_file` | download_mode handling copies the counterpart |
| `onedrive_upload_file` | `drive_upload_file` | popup | `upload_file` | `parent_folder_id="root"`, `name`, file source, `conflict="fail"` (`fail`/`rename`/`replace`) |
| `onedrive_move_file` | `drive_move_file` | popup | `move_file` | `file_id`, `destination_folder_id`, `new_name=""` |
| `onedrive_write_file_content` | `drive_write_file_content` | popup | `write_file` | `file_id`, `content`. Refused before the gate unless the item's mime type starts with `text/` or its name ends in `.txt`, `.md`, `.csv` or `.json`. |
| `onedrive_excel_get_metadata` | `drive_sheets_get_metadata` | auto | — | `file_id` |
| `onedrive_excel_get_values` | `drive_sheets_get_values` | review | `read_values` | `file_id`, `range` as `Sheet!A1:B2` |
| `onedrive_excel_write_range` | `drive_sheets_write_range` | popup | `write_range` | `values` is a JSON 2-D array |
| `onedrive_excel_add_sheet` | `drive_sheets_add_sheet` | popup | `add_sheet` | `title` |
| `onedrive_excel_rename_sheet` | `drive_sheets_rename_sheet` | popup | `rename_sheet` | `sheet`, `new_title` |
| `onedrive_excel_format_range` | `drive_sheets_format_range` | popup | `format_range` | `bold`, `italic`, `font_color`, `fill_color` (`#RRGGBB`), `number_format` |
| `onedrive_excel_insert_dimensions` | `drive_sheets_insert_dimensions` | popup | `insert_dimensions` | `range`, `shift` (`down`/`right`) |
| `onedrive_excel_delete_dimensions` | `drive_sheets_delete_dimensions` | popup | `delete_dimensions` | `shift` (`up`/`left`); `destructive=True` |

Excel tools accept only `.xlsx` items; any other item is rejected before the gate with
"`onedrive_excel_*` works on .xlsx workbooks only." Reads go through `apply_text("onedrive_privacy",
...)` with the counterpart's categories. **Word documents have no Graph editing API**, so there is
no counterpart for `drive_write_doc_content`, `drive_docs_*`, `drive_add_comment`,
`drive_create_blank_file`, `drive_sheets_create` or `drive_list_shared_drives`.

### 3.11 Outlook Mail (`outlook_mail_client.py` p11–p12, `connectors/outlook_mail.py` p21–p23)

**Client — reads (p11).**

- **Dataclasses.**
  - `OutlookMessage(id, conversation_id, subject, sender, to: list[str], cc: list[str],
    received, is_read, has_attachments, categories: list[str], folder_id, snippet, body_text,
    body_html, web_link)`.
    - Addresses render as `Name <address>`.
    - `body_html` is `body.content` when `contentType == "html"`; `body_text` is the content when
      `contentType == "text"`, else `""`.
  - `OutlookAttachment(id, name, content_type, size, is_inline)`. **Content is intentionally never
    carried** in this dataclass.
  - `OutlookFolder(id, name, parent_id, unread_count, total_count)`.
  - `OutlookCategory(name, color)`.
  - `OutlookRule(id, name, sequence, is_enabled, conditions: dict, actions: dict)`.
  - Static parsers `_parse_message`, `_parse_attachment`, `_parse_folder`, `_parse_category` and
    `_parse_rule` take raw dicts.
- **Methods.**
  - `list_messages(folder="inbox", query="", max_results=20, unread_only=False)`:
    - `$search` with `"{query}"` quoted when there is a query; `$orderby=receivedDateTime desc`
      only when there isn't (Graph rejects the two together).
    - `$filter=isRead eq false` when `unread_only` is true.
    - `$select=id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,hasAttachments,categories,parentFolderId,bodyPreview,webLink`.
  - `list_conversations(folder, max_results)`: `list_messages` over `max_results * 3` rows, grouped
    by `conversation_id`, keeping the newest message per conversation.
  - `get_message(id)`.
  - `list_conversation_messages(conversation_id, max_results=50)`: `$filter=conversationId eq
    '{id}'`, sorted by `received` in Python.
  - `list_attachments(message_id)`: `$select=id,name,contentType,size,isInline`.
  - `get_attachment_bytes(message_id, attachment_id, max_bytes)`: `get_bytes(.../attachments/{id}/$value)`.
  - `list_folders()`: `list_all("/me/mailFolders", {"$top": "100"})`.
  - `list_categories()`.
  - `list_rules()`.
  - `resolve_folder(name_or_id)`: accepts a well-known name (`inbox`, `archive`, `drafts`,
    `sentitems`, `deleteditems`, `junkemail`), an id, or a display name.

**Client — writes (p12).**

- **Drafts.**
  - `create_draft(to, subject, body_markdown, cc="", bcc="")`: POST `/me/messages` with `body:
    {"contentType": "html", "content": markdown_to_html(body_markdown)}`, using `email_markdown`
    as `gmail_client.py:33` does. It never sends: **no method calls `/send`** (ADR 0114 records
    the `Mail.Send` omission).
  - `create_reply_draft(message_id, body_markdown, reply_all: bool)`: POST
    `/me/messages/{id}/createReply` or `createReplyAll` with `{}`. Then it PATCHes the draft's
    `body` to the rendered HTML followed by the draft's original quoted body.
  - `add_attachment(draft_id, name, data, content_type)`:
    - `len(data) <= 3 * 1024 * 1024`: POST `/me/messages/{id}/attachments` with `{"@odata.type":
      "#microsoft.graph.fileAttachment", "name", "contentType", "contentBytes": base64}`.
    - Otherwise `upload_large(f"/me/messages/{id}/attachments/createUploadSession",
      {"AttachmentItem": {"attachmentType": "file", "name", "size"}}, data)`.
- **Organizing.**
  - `move_message(message_id, destination_id)`: POST `/move` `{"destinationId"}`.
  - `archive_message(id)`: `move_message(id, "archive")`.
  - `set_categories(message_id, categories)`: PATCH.
  - `create_category(name, color="preset0")`: POST `/me/outlook/masterCategories`.
- **Rules.** `create_rule(name, conditions, actions)` and `update_rule(rule_id, name, conditions,
  actions)`: POST and PATCH `/me/mailFolders/inbox/messageRules`, with `sequence = max(existing) +
  1` on create and `isEnabled: true`.

**Tools.**

| Tool | Counterpart | Gate | Op suffix | Notes |
|---|---|---|---|---|
| `outlook_mail_list_messages` | `gmail_list_messages` | auto | — | `folder="inbox"`, `query=""`, `max_results=20`, `unread_only=False` |
| `outlook_mail_list_conversations` | `gmail_list_threads` | auto | — | `folder="inbox"`, `max_results=20` |
| `outlook_mail_list_message_attachments` | `gmail_list_message_attachments` | auto | — | `message_id` |
| `outlook_mail_list_folders` | `gmail_list_labels` | auto | — | |
| `outlook_mail_list_categories` | `gmail_list_labels` | auto | — | |
| `outlook_mail_list_rules` | `gmail_list_filters` | auto | — | |
| `outlook_mail_get_message` | `gmail_get_message` | review | `read_message` | `message_id`, `include_html=False` |
| `outlook_mail_get_conversation` | `gmail_get_thread` | review | `read_conversation` | `conversation_id`. Its verb is `TOOL_TO_VERB["gmail_get_thread"]`. |
| `outlook_mail_download_attachment` | `gmail_download_attachment` | review | `download_attachment` | `message_id`, `attachment_id` (replaces `attachment_name`), `destination_dir=""` |
| `outlook_mail_create_draft` | `gmail_create_draft` | popup | `create_draft` | same params |
| `outlook_mail_reply_draft` | `gmail_reply_draft` | popup | `create_draft` | same |
| `outlook_mail_reply_all_draft` | `gmail_reply_all_draft` | popup | `create_draft` | same |
| `outlook_mail_create_draft_with_attachments` | `gmail_create_draft_with_attachments` | popup | `create_draft` | same |
| `outlook_mail_reply_draft_with_attachments` | `gmail_reply_draft_with_attachments` | popup | `create_draft` | same |
| `outlook_mail_reply_all_draft_with_attachments` | `gmail_reply_all_draft_with_attachments` | popup | `create_draft` | same |
| `outlook_mail_add_category` | `gmail_add_label` | popup | `add_category` | `message_id`, `category_name` |
| `outlook_mail_remove_category` | `gmail_remove_label` | popup | `remove_category` | `message_id`, `category_name` |
| `outlook_mail_create_category` | `gmail_create_label` | popup | `create_category` | `name`, `color="preset0"` (`preset0`–`preset24`) |
| `outlook_mail_archive_message` | `gmail_archive_message` | popup | `archive_message` | `message_id` |
| `outlook_mail_move_message` | — | popup | `move_message` | `message_id`, `destination_folder` (id, display name or well-known name). Verb `MOVE`, layout `NARROW`. Effect: "The message moves to that folder." Card preview: From, Subject, From folder, To folder. |
| `outlook_mail_create_rule` | `gmail_create_filter` | popup | `create_rule` | `name`, `from_address`, `to_address`, `subject`, `query` (→ `bodyOrSubjectContains`), `has_attachment`, `add_category_names` (→ `assignCategories`), `move_to_folder` (→ `moveToFolder`), `archive` (→ `moveToFolder` archive id), `mark_as_read`, `mark_important` (→ `markImportance: "high"`) |
| `outlook_mail_update_rule` | `gmail_update_filter` | popup | `update_rule` | `rule_id` plus the same fields. Graph updates the rule in place, so the id is kept. |

**Rules never forward, redirect or delete** (a design choice, below). The rule tools have no
`forward_to` and send no forwarding, redirect or delete action.

p21 does the six list tools, `get_message`, `get_conversation` and `download_attachment`. p22 does
the six draft tools. p23 does the rest.

### 3.12 Always-allow scopes (p25–p26)

Every new predicate is a **new** `ScopeSelector` in `policy/scopes.py`'s `NEW_SCOPE_SELECTORS`;
`SCOPE_SELECTORS` is frozen against `tests/unit/policy/_v1_reference.py`. Each one gets a
`PROPOSABLE_SCOPES` entry built with `_scope(...)` and a `catalogue.VALUE_HINTS` example.

| Predicate | Scope type | Kind / resolves | Connector | Verbs | Value builder / match | Mirrors |
|---|---|---|---|---|---|---|
| `approved_todo_list` | `todo.list` | identity / args | todo | CREATE, UPDATE, COMPLETE, MOVE | `_task_list_ids` (reused as is) | `approved_task_list` |
| `approved_outlook_calendar` | `outlook_calendar.calendar` | identity / args | outlook_calendar | READ, CREATE, UPDATE, DELETE | `_args_values("calendar_id")`; excludes `outlook_calendar.out_of_office` | `personal_calendar` |
| `i_am_outlook_organizer` | `outlook_calendar.organized_by_me` | attribute / fetched | outlook_calendar | READ | `raw_data.organizer_email == ctx.my_email` | `i_am_organizer` |
| `outlook_contacts_category_allowlist` | `outlook_contacts.category` | identity / args | outlook_contacts | LABEL | `_args_values("category_name")` | `label_name_allowlist` (contacts entry) |
| `trusted_outlook_sender_domain` | `outlook_mail.sender_domain` | attribute / fetched | outlook_mail | READ, DOWNLOAD, ARCHIVE | `_sender_domain` over `raw_data.sender` | `trusted_sender_domain` |
| `outlook_mail_category_allowlist` | `outlook_mail.category` | identity / args | outlook_mail | LABEL | `_args_values("category_name")` | `label_name_allowlist` (gmail entry) |
| `always_allow` under `outlook_mail.anything` | `outlook_mail.anything` | — | outlook_mail | DRAFT | `widenable=False`, `group="outlook_mail.anything"` | the `gmail.anything` entry |
| `approved_onedrive_folder` | `onedrive.folder` | identity / fetched | onedrive | READ, DOWNLOAD | the item's `parent_id` | `approved_folder` |
| `approved_onedrive_sandbox_folder` | `onedrive.folder` | identity / fetched | onedrive | UPDATE, FORMAT, RESTRUCTURE, DELETE | same | `approved_sandbox_folder` |
| `onedrive_parent_folder_allowlist` | `onedrive.folder` | identity / args | onedrive | CREATE | `_args_values("parent_folder_id")` | `parent_folder_allowlist` |
| `onedrive_move_within_approved_folders` | `onedrive.folder` | identity / fetched | onedrive | MOVE | source parent plus `destination_folder_id` | `move_within_approved_folders` |

- **Contacts edit.** `outlook_contacts.edit` gets the same condition-scope treatment as
  `contacts.edit` (the `no_contact_info_change` condition), copied from however `contacts.edit`
  is expressed in `propose.py`/`catalogue.py`.
- **Mail rules.** `outlook_mail.create_rule`/`update_rule` get the treatment
  `gmail.create_filter`/`update_filter` get: configurable only from Settings through an
  `outlook_mail.anything` extra scope in `catalogue.EXTRA_SCOPES`, never proposed from a popup.
- **Grant resource types** (`policy/resource_registry.GRANT_RESOURCE_TYPES`) are added for
  `todo.task_lists`, `outlook_calendar.calendars` and `onedrive.folders`. They mirror the
  `tasks`, `calendar` and `drive` entries, and their resolvers call `client.list_task_lists()`,
  `client.list_calendars()` and `client.get_item(id)` respectively.

## 4. ADRs

- **ADR 0114** — The Microsoft connectors share one Entra grant, token file and full-family scope
  set, requested in one consent. `Mail.Send` is never requested, so no tool can send mail.
  Rejected alternatives: a grant per connector, and incremental scopes per enabled connector.
- **ADR 0115** — The Entra client id comes from the organization config's `microsoft` section;
  PrivacyFence ships no Microsoft client id. Rejected: a built-in multi-tenant client id like
  Telegram's (ADR 0040). That question stays with
  https://github.com/privacyfence/privacyfence/issues/828.
- **ADR 0116** — Graph is called through one `requests`-based transport (`msgraph_http.py`)
  rather than `msal` or `msgraph-sdk`. Rejected: both SDKs are new dependencies, and msgraph-sdk
  is async/kiota-generated and heavy.
- **ADR 0117** — The loopback sign-in redeems as a public client (PKCE, no secret). Org mode's web
  callback redeems as a confidential client with the bundle's `client_secret`. The token file
  records which, and refresh follows it.
- **ADR 0118** — Graph gaps are approximated in the open, never silently:
  - conversations stand in for threads;
  - categories and folders stand in for labels;
  - `todo_move_task` re-creates the task and deletes the original, refusing when that would lose
    attachments or checklist steps;
  - out of office is an `oof` event, not automatic replies;
  - Outlook rules can never forward, redirect or delete, though Gmail filters can forward
    (Outlook has no verified-forwarding-address check).
- **ADR 0119** — Microsoft live fixtures are recorded from raw Graph reads through the shared
  transport, before the clients exist, with a Graph-specific redaction pass. Rejected: recording
  through client methods as the other connectors do, which would put fixtures after the parsers
  they are meant to shape.

## 5. Manual steps

Step-by-step page: see `manual_steps_artifact` in the manifest (source:
`docs/microsoft-connectors-plan-manual-steps.html`).

- **Before `/implement`:**
  - a dedicated free outlook.com QA account;
  - an Azure account and the "PrivacyFence QA" Entra app registration;
  - the QA seed data;
  - the `qa_environment.yaml` sections on the runner.
- **At the one pause, after p04 merges** (the maintainer chose this over a device-code bootstrap):
  - build and install the QA bundle with the new `--microsoft-client-id` flag;
  - run `qa_authenticate_connectors.py --only microsoft` from the feature branch;
  - copy the token to the runner.
- **After the last phase:** the end-to-end smoke test on the QA account in local mode.

## 6. Risks and open questions

- **Excel on a personal OneDrive.** If `check_onedrive_excel` fails with a Graph error saying the
  workbook API is not supported for this account, p05 stops with `status=blocked` and quotes the
  error. The maintainer then decides whether p10 and p20 move to
  https://github.com/privacyfence/privacyfence/issues/828.
- **Redirect URI mismatch** (`AADSTS50011`) at the pause means the app registration lacks
  `http://localhost:53685/callback`. The artifact has the maintainer register both
  `http://localhost/callback` and `http://localhost:53685/callback`.
- **A fixture field the redaction doesn't know.** If p05's diff review finds a real address,
  name, tenant or account id in a recorded fixture, the worker fixes `redact_msgraph` and
  re-records. It does not commit the leak. If the field can't be classified by the §3.5 rules,
  it stops with `status=blocked`.
- **Graph shape differs from §3.** When a recorded fixture contradicts a field path in §3.7–§3.11
  (for example, `wellknownListName` is missing), the client phase follows the fixture, says so in
  its report, and keeps `TestLiveFixtureParsing` green. When the difference changes a tool's
  params or gate, it stops with `status=blocked`.
- **The policy layer's invariants** (`test_propose.py`'s "every proposal accepts its own item",
  `test_scopes.py`'s classification tests, `test_catalogue.py`'s coverage) are the likeliest place
  for a phase to be wrong. p25 and p26 run on Opus for that reason.
- **Workflow queueing.** `qa-record-fixture.yml` runs share one concurrency group, and GitHub
  keeps only one *pending* run per group: dispatching several at once cancels all but the newest
  pending one. p05 dispatches one connector at a time and waits for each run to finish.

## 7. Implementation manifest

```yaml
plan_slug: microsoft-connectors
feature_branch: feature/microsoft-connectors
tracking_issue: 688
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/EUzH5txxppNwvaP4DaLcg5
manual_steps_source: docs/microsoft-connectors-plan-manual-steps.html
manual_before:
  - id: mb1-qa-account
    title: Create a dedicated free outlook.com QA account
    why: p05 records fixtures from it and the final smoke test uses it; a personal or production mailbox must never be the QA account.
    done_when: You can sign in to outlook.live.com with the new account and it has OneDrive and To Do.
  - id: mb2-app-registration
    title: Register the "PrivacyFence QA" Entra app (personal + organizational accounts, localhost redirects, delegated Graph permissions)
    why: The pause after p04 signs the QA account in through this app; without it there is no client id and no token, and p05 cannot record.
    done_when: The app's Overview page shows an Application (client) ID, and Authentication lists http://localhost/callback and http://localhost:53685/callback under "Mobile and desktop applications".
  - id: mb3-seed-data
    title: Seed the QA account (mail, folder, category, rule, calendar event, contact, OneDrive files, To Do lists)
    why: Every check in p04/p05 resolves a seed item by its exact name and refuses to record anything untagged.
    done_when: Every item in the artifact's seed checklist exists with exactly the listed name.
  - id: mb4-runner-manifest
    title: Add the Microsoft sections to ~/privacyfence/tests/fixtures/qa_environment.yaml on the runner
    why: qa-record-fixture.yml copies this file into its checkout; without the sections the recorder falls back to defaults, and any renamed seed item would fail.
    done_when: The runner's qa_environment.yaml contains outlook_mail, outlook_calendar, outlook_contacts, onedrive, onedrive_excel and todo sections matching the plan's section 3.5.
manual_after:
  - id: ma1-smoke-test
    title: End-to-end smoke test of all five connectors in local mode against the QA account
    why: Nothing before this exercises the real consent screen from Settings, the approval cards with real Graph data, or a real MCP client driving the tools.
verify_after_merge:
  - python3 -m pytest tests/unit/test_qa_fixture_recorder.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_website_connector_pages.py -q
final_checks:
  - docs/microsoft-connectors-plan.md and docs/microsoft-connectors-plan-manual-steps.html are deleted and nothing links to them
  - ADRs 0114, 0115, 0116, 0117, 0118 and 0119 exist, are Accepted, and are in docs/adr/README.md's index (renumbered if main took a number first)
  - CHANGELOG.md has [Unreleased] entries and no version heading
  - p28's PHASE-REPORT links a green connector-live-check.yml run on the feature branch, and the PR body carries it under "## Local QA check"
  - grep -rn "/send\b" src/privacyfence/outlook_mail_client.py finds nothing
phases:
  - id: p01-msgraph-oauth
    title: Microsoft sign-in, token file, --microsoft-oauth, org bundle flags and the QA auth step
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/msgraph_oauth.py
      - src/privacyfence/daemon_main.py
      - scripts/build_org_bundle.py
      - scripts/qa_authenticate_connectors.py
      - tests/unit/test_msgraph_oauth.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_build_org_bundle.py
      - tests/unit/test_qa_authenticate_connectors.py
      - tests/unit/test_systemic_gate_invariants.py
      - docs/microsoft-365-setup.md
      - docs/README.md
    brief: |
      1. Create src/privacyfence/msgraph_oauth.py exactly as the plan's section 3.3 specifies (constants,
         MicrosoftOAuthError, every function and its behavior). Use requests with timeout=30 and
         oauth_loopback.run_browser_oauth. Model the module docstring and structure on
         src/privacyfence/atlassian_oauth.py, but note the differences: public client, no secret on the
         loopback, form-encoded token requests, redirect_host="localhost".
      2. daemon_main.py: add TOKEN_FILES["microsoft"], run_microsoft_oauth(org_config) and the
         --microsoft-oauth flag, wired exactly like --atlassian-oauth (daemon_main.py:1750-1766, 2071,
         2179-2184, 2212-2213). Import msgraph_oauth's functions the way atlassian_oauth's are imported
         (:141-143).
      3. scripts/build_org_bundle.py: add the "Microsoft (Outlook, OneDrive, To Do)" argument group and
         the bundle["microsoft"] section, _CONNECTOR_CALLBACKS["microsoft"] and the services tuple
         entry, as section 3.3 says.
      4. scripts/qa_authenticate_connectors.py: append the Microsoft OAuthStep from section 3.3.
      5. tests/unit/test_systemic_gate_invariants.py: add ("msgraph_oauth", None, "save_token_file") to
         TOKEN_WRITE_SITES.
      6. Tests: create tests/unit/test_msgraph_oauth.py, modeled on tests/unit/test_atlassian_oauth.py
         (TestLoadSaveTokenFile, TestRefresh, TestAuthorizeInteractiveBuildUrl, TestAuthorizeInteractiveExchange,
         TestTokenRecord, TestFetchAccountEmail). Include negative tests: client_secret absent from the
         form when empty and present when given; an error message never contains the code or token; a
         missing token file raises with "Use Authenticate…". Extend tests/unit/test_daemon_main.py
         (the --microsoft-oauth flag and run_microsoft_oauth with no config, with success, and with
         MicrosoftOAuthError), tests/unit/test_build_org_bundle.py (the section and the usage error) and
         tests/unit/test_qa_authenticate_connectors.py (the group list gains "microsoft").
      7. Create docs/microsoft-365-setup.md: "Register the app" (Entra admin center → App
         registrations → New registration; "Accounts in any organizational directory and personal
         Microsoft accounts"; platform "Mobile and desktop applications" with http://localhost/callback
         and http://localhost:53685/callback; API permissions: the delegated scopes in section 3.3's
         DEFAULT_SCOPES; no secret for local mode), "Install the organization config"
         (build_org_bundle.py --microsoft-client-id [--microsoft-tenant]), "Org mode" (add a Web
         platform redirect <issuer>/oauth/callback/microsoft, create a client secret, pass
         --microsoft-client-secret), "Sign in" (Settings → Authenticate…, or --microsoft-oauth), and
         "Personal and work accounts" (verified on personal accounts; work/school accounts are tracked in
         https://github.com/privacyfence/privacyfence/issues/828). Mirror docs/atlassian-setup.md's
         structure and tone. Link it from docs/README.md's published (user) half next to
         atlassian-setup.md.
      Stop with status=blocked if run_browser_oauth's signature differs from
      oauth_loopback.py:148-156 as quoted in the plan's section 2.
    acceptance:
      - python3 -m pytest tests/unit/test_msgraph_oauth.py tests/unit/test_daemon_main.py tests/unit/test_build_org_bundle.py tests/unit/test_qa_authenticate_connectors.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit/test_build_site.py -q passes (docs/microsoft-365-setup.md is published and linked)
      - grep -n "53685" src/privacyfence/msgraph_oauth.py finds MICROSOFT_OAUTH_PORT
      - python3 -m privacyfence.daemon_main --help | grep -- --microsoft-oauth prints a line
  - id: p02-graph-http
    title: Shared Graph transport (msgraph_http) and agent-facing unavailable errors (msgraph_errors)
    depends_on: [p01-msgraph-oauth]
    complexity: M
    touches:
      - src/privacyfence/msgraph_http.py
      - src/privacyfence/msgraph_errors.py
      - tests/unit/test_msgraph_http.py
      - tests/unit/test_msgraph_errors.py
      - tests/conftest.py
    brief: |
      1. Create src/privacyfence/msgraph_http.py exactly as the plan's section 3.4 specifies (constants,
         GraphError, GraphHttp and every method's behavior, the host check, the refresh lock and
         re-read-first refresh, 401 retry, 429/503/504 Retry-After handling, the error mapping,
         list_all, get_bytes, put_bytes, upload_large, check_connection). The module docstring
         explains the single choke point (request) and why the bearer token never leaves
         graph.microsoft.com.
      2. Add msgraph_http._REFRESH_LOCKS.clear() to tests/conftest.py's _reset().
      3. Create src/privacyfence/msgraph_errors.py exactly as section 3.4 specifies, mirroring
         src/privacyfence/google_errors.py:109-175.
      4. tests/unit/test_msgraph_http.py: use a fake requests.Session (a small class recording calls and
         returning scripted responses) and injected sleep/clock. Test classes: TestRequestSuccess (JSON,
         204→None), TestHostCheck (a nextLink or path on another host raises and sends no request),
         TestProactiveRefresh (expiry inside the margin refreshes first), TestUnauthorizedRetry (401 →
         refresh → retry once; a second 401 raises with status 401), TestConcurrentRefreshAdoption (a
         token file already refreshed by another client is adopted without calling
         msgraph_oauth.refresh), TestConfidentialRefresh (client_secret is passed only when client_kind
         is confidential), TestThrottle (Retry-After honored and capped; gives up after 3), TestErrorMapping
         (JSON error body, non-JSON body, network error), TestListAll, TestGetBytes (the size cap),
         TestUploadLarge (chunk ranges; no Authorization header on chunk PUTs), TestNotAuthenticated
         (a missing token file → error_cls with status 401).
      5. tests/unit/test_msgraph_errors.py: 404 and each NOT_FOUND code, 403 with each FORBIDDEN code,
         403 with another code → None, 500 → None, the account formatting.
    acceptance:
      - python3 -m pytest tests/unit/test_msgraph_http.py tests/unit/test_msgraph_errors.py -q passes
      - grep -n "Authorization" src/privacyfence/msgraph_http.py shows the header set only inside request()
      - python3 scripts/mypy_strict_modules.py passes
  - id: p03-family-wiring
    title: Microsoft sign-in from Settings and org-mode connect (no connectors yet)
    depends_on: [p01-msgraph-oauth]
    complexity: M
    touches:
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_connect.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_routes_connect.py
    brief: |
      1. settings_controller.py: add MICROSOFT_CONNECTORS, the ORG_BUNDLE_SERVICES entry, the
         authenticate_connector branch and _authenticate_microsoft exactly as the plan's section 3.6
         says, mirroring _authenticate_atlassian (settings_controller.py:1297-1344).
      2. web/routes_connect.py: add MICROSOFT_SERVICES (empty), the _GRANT_KEY/_ORG_CONFIG_SECTION
         entries, _is_configured's client_id+client_secret rule, the authorize URL and exchange branches
         (confidential redemption; token_record with client_kind "confidential"), and the page rows
         rendering MICROSOFT_SERVICES, as section 3.6 says. Mirror the Atlassian branches (:99-148,
         :275-326, :338-390, :595-676).
      3. Tests: tests/unit/test_settings_controller.py gains TestAuthenticateMicrosoft (mirroring
         TestAuthenticateAtlassian :1215, with MICROSOFT_CONNECTORS monkeypatched to {"todo"}) and a
         dispatch case in TestAuthenticateDispatch. tests/unit/web/test_routes_connect.py gains, with
         MICROSOFT_SERVICES monkeypatched to frozenset({"todo"}): start redirects to
         login.microsoftonline.com with the /oauth/callback/microsoft redirect_uri; the callback
         exchanges with the client_secret and saves client_kind "confidential"; a callback on the wrong
         grant URL is rejected; without client_secret the row shows "Not set up by your organization".
    acceptance:
      - python3 -m pytest tests/unit/test_settings_controller.py tests/unit/web/test_routes_connect.py -q passes
      - grep -n "MICROSOFT_SERVICES" src/privacyfence/web/routes_connect.py shows the frozenset and its uses
  - id: p04-recorder-checks
    title: QA recorder checks for the Microsoft family (not registered until fixtures are recorded)
    depends_on: [p02-graph-http]
    complexity: M
    human_gate: true
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/qa_environment.yaml.example
      - docs/connector-qa.md
    brief: |
      1. scripts/qa_fixture_recorder.py: import GraphHttp and GraphError from privacyfence.msgraph_http
         next to the other client imports (:75-88). Add _build_msgraph_http, redact_msgraph (with its
         key-set constants next to the other redaction constants), _keep_tagged, and check_outlook_mail,
         check_outlook_calendar, check_outlook_contacts, check_onedrive, check_onedrive_excel and
         check_todo, exactly as the plan's section 3.5 specifies (calls, guardrails, fixture names,
         recording pipeline raw = deidentify_structural_fields(redact_msgraph(value))). Each returns
         list[CheckResult] like check_tasks (:1237-1290), and catches GraphError into a failed
         CheckResult.
      2. Do NOT add them to CONNECTOR_CHECKS or EXPECTED_FIXTURES yet: TestFixturePresence would fail
         until the fixtures exist (p05 registers and records them). Instead add, right after
         EXPECTED_FIXTURES:
           MSGRAPH_CHECKS: dict[str, Callable[[bool, dict], list[CheckResult]]] = {...the six...}
           MSGRAPH_EXPECTED_FIXTURES: dict[str, tuple[str, ...]] = {...the fixture names from 3.5...}
         and an import-time assert that their keys are equal.
      3. tests/fixtures/qa_environment.yaml.example: add the six sections from section 3.5, each with the
         same comment style pointing at docs/connector-qa.md's "Seed: <X>" heading.
      4. docs/connector-qa.md: add a "Microsoft" row to "QA accounts" (a dedicated free outlook.com
         account; the Entra app from microsoft-365-setup.md), a "Seed: Outlook Mail", "Seed: Outlook
         Calendar", "Seed: Outlook Contacts", "Seed: OneDrive" and "Seed: To Do" checklist each (items
         and exact names from the plan's manual steps page source, docs/microsoft-connectors-plan-manual-steps.html,
         section mb3), the six rows in "Manifest reference", the microsoft group in "Authenticating
         connectors", and a sentence in "Running the recorder" that the Microsoft checks read raw Graph
         JSON through msgraph_http (ADR 0119).
      5. tests/unit/test_qa_fixture_recorder.py: TestRedactMsgraph (a synthetic sample containing every
         key class in section 3.5: emailAddress dicts, each identity-set key, each @odata key, id-suffixed
         and listed id keys, a non-string id-suffixed value left alone, URL keys blanked after
         deidentify), TestKeepTagged, and one TestCheck<Name> class per check with a fake GraphHttp
         (a class whose get(path, params) returns scripted JSON keyed by path): the happy path records
         every fixture; a resolve miss yields one failed result and no further calls; an untagged item is
         never recorded; list fixtures contain only kept items. Add TestMsgraphRegistries (keys equal).
    acceptance:
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q passes
      - python3 scripts/qa_fixture_recorder.py --help exits 0
      - grep -c "check_outlook_mail\|check_outlook_calendar\|check_outlook_contacts\|check_onedrive\b\|check_onedrive_excel\|check_todo" scripts/qa_fixture_recorder.py is at least 12
  - id: p05-record-fixtures
    title: Register the Microsoft checks and record their fixtures on the self-hosted runner
    depends_on: [p04-recorder-checks]
    complexity: S
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/live/outlook_mail/**
      - tests/fixtures/live/outlook_calendar/**
      - tests/fixtures/live/outlook_contacts/**
      - tests/fixtures/live/onedrive/**
      - tests/fixtures/live/onedrive_excel/**
      - tests/fixtures/live/todo/**
    brief: |
      This phase starts only after the p04 human gate, at which the maintainer installed the QA bundle
      (with its microsoft section) and credentials/microsoft_token.json on the runner.
      1. scripts/qa_fixture_recorder.py: move the six MSGRAPH_CHECKS entries into CONNECTOR_CHECKS and the
         six MSGRAPH_EXPECTED_FIXTURES entries into EXPECTED_FIXTURES, each on its own line in exactly the
         form `    "outlook_mail": check_outlook_mail,` (four spaces, double quotes) because
         qa-record-fixture.yml validates the name with grep "^    \"<name>\": check_". Delete
         MSGRAPH_CHECKS, MSGRAPH_EXPECTED_FIXTURES and their assert, and TestMsgraphRegistries.
      2. Commit and push the phase branch.
      3. For each name in order todo, outlook_contacts, outlook_calendar, onedrive, onedrive_excel,
         outlook_mail: dispatch .github/workflows/qa-record-fixture.yml (GitHub MCP actions_run_trigger)
         with ref = this phase branch and connector = the name, then wait until that run completes
         before dispatching the next one (use the Monitor tool or send_later check-ins; never sleep; never
         dispatch two at once: the shared concurrency group keeps only one pending run and cancels the
         rest). Each run commits tests/fixtures/live/<name>/ back to this branch.
      4. git pull. Read every fixture diff as docs/connector-qa.md "Reviewing recorded fixtures" requires:
         no real address, display name, tenant, account id, token or private content. A leak means: fix
         redact_msgraph (and its test), push, and re-dispatch that one connector.
      5. Run /dod; TestFixturePresence must now pass with the new directories.
      Stop with status=blocked, quoting the run URL and the error line from its log, if any run fails
      (an expired or missing grant, an unresolved seed item, or check_onedrive_excel reporting that the
      workbook API is unsupported for this account). Do not retry a failed run more than once.
    acceptance:
      - ls tests/fixtures/live/{outlook_mail,outlook_calendar,outlook_contacts,onedrive,onedrive_excel,todo}/*.json lists exactly the files in the plan's section 3.5
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q passes, including TestFixturePresence
      - grep -rniE "@(outlook|hotmail|live)\.com|onmicrosoft" tests/fixtures/live/{outlook_mail,outlook_calendar,outlook_contacts,onedrive,onedrive_excel,todo} finds nothing
      - the PHASE-REPORT lists the six run URLs
  - id: p06-todo-client
    title: To Do client
    depends_on: [p05-record-fixtures]
    complexity: M
    touches:
      - src/privacyfence/todo_client.py
      - tests/unit/test_todo_client.py
    brief: |
      1. Create src/privacyfence/todo_client.py per the plan's section 3.7 (TodoClientError(GraphError),
         dataclasses, static parsers, every method) and section 3.6's constructor rule
         (TodoClient(org_section, token_file) building GraphHttp(..., error_cls=TodoClientError);
         check_connection()). Read tests/fixtures/live/todo/*.json first; where a field path differs from
         section 3.7, follow the fixture and note it in your report.
      2. tests/unit/test_todo_client.py: a fake GraphHttp injected by monkeypatching the client's _http;
         one test class per method (payload shape, blank-means-unchanged, the status values, the filter
         for show_completed), TestMoveTask (copy then delete; refusal with attachments; refusal with
         checklist items; no delete when the create fails), and TestLiveFixtureParsing mirroring
         tests/unit/test_tasks_client.py:564-586 over list_task_lists.json, list_tasks.json and
         get_task.json, with assert_no_placeholder_fields from tests/helpers.py on the parsed task.
      Stop with status=blocked if a fixture shows To Do has a native move endpoint.
    acceptance:
      - python3 -m pytest tests/unit/test_todo_client.py -q passes with no skips in TestLiveFixtureParsing
      - grep -n "class TodoClientError(GraphError)" src/privacyfence/todo_client.py matches
  - id: p07-contacts-client
    title: Outlook Contacts client
    depends_on: [p05-record-fixtures]
    complexity: M
    touches:
      - src/privacyfence/outlook_contacts_client.py
      - tests/unit/test_outlook_contacts_client.py
    brief: |
      1. Create src/privacyfence/outlook_contacts_client.py per the plan's section 3.8 and section 3.6's
         constructor rule. Import _parse_json_list from privacyfence.connectors.contacts only if it is a
         plain module-level function; otherwise copy its logic into a private helper here. Read
         tests/fixtures/live/outlook_contacts/*.json first.
      2. tests/unit/test_outlook_contacts_client.py: the search filter string (including quote
         doubling), the field mapping for create/update (each phone type), set_categories, and
         TestLiveFixtureParsing over list_contacts.json and get_contact.json with
         assert_no_placeholder_fields.
    acceptance:
      - python3 -m pytest tests/unit/test_outlook_contacts_client.py -q passes with no skips in TestLiveFixtureParsing
  - id: p08-calendar-client
    title: Outlook Calendar client
    depends_on: [p05-record-fixtures]
    complexity: M
    touches:
      - src/privacyfence/outlook_calendar_client.py
      - tests/unit/test_outlook_calendar_client.py
    brief: |
      1. Create src/privacyfence/outlook_calendar_client.py per the plan's section 3.9 and section 3.6's
         constructor rule. Bodies go through privacyfence.html_to_text.html_to_text. Read
         tests/fixtures/live/outlook_calendar/*.json first.
      2. tests/unit/test_outlook_calendar_client.py: default-calendar paths for a blank calendar_id;
         calendarView params; the create payload (attendees, all-day); update sends only the changed
         fields; the sensitivity values; the out-of-office payload (showAs oof, isAllDay for date
         values, default calendar); list_categories; and TestLiveFixtureParsing over
         list_calendars.json, list_events.json and get_event.json with assert_no_placeholder_fields.
    acceptance:
      - python3 -m pytest tests/unit/test_outlook_calendar_client.py -q passes with no skips in TestLiveFixtureParsing
  - id: p09-onedrive-client
    title: OneDrive client (files)
    depends_on: [p05-record-fixtures]
    complexity: M
    touches:
      - src/privacyfence/onedrive_client.py
      - tests/unit/test_onedrive_client.py
    brief: |
      1. Create src/privacyfence/onedrive_client.py with the file half of the plan's section 3.10
         (OneDriveItem, _parse_item, list_files, list_folder, get_item, download, upload,
         move, write_content) and section 3.6's constructor rule. Read
         tests/fixtures/live/onedrive/*.json first.
      2. tests/unit/test_onedrive_client.py: the search quote doubling; root vs item children paths;
         the small upload path vs upload_large at the SIMPLE_UPLOAD_MAX_BYTES boundary; the conflict
         behavior in the URL; move with and without new_name; download passes max_bytes; and
         TestLiveFixtureParsing over get_folder.json, list_folder.json and get_file_metadata.json with
         assert_no_placeholder_fields.
    acceptance:
      - python3 -m pytest tests/unit/test_onedrive_client.py -q passes with no skips in TestLiveFixtureParsing
  - id: p10-onedrive-excel-client
    title: OneDrive client (Excel workbook methods)
    depends_on: [p09-onedrive-client]
    complexity: M
    touches:
      - src/privacyfence/onedrive_client.py
      - tests/unit/test_onedrive_client.py
    brief: |
      1. Add the Excel half of the plan's section 3.10 to src/privacyfence/onedrive_client.py
         (OneDriveWorksheet, _parse_worksheet, _parse_range_values, and the eight workbook methods),
         under a "# ----- Excel workbook ----- #" banner. Read tests/fixtures/live/onedrive_excel/*.json first.
      2. Extend tests/unit/test_onedrive_client.py: each method's path and body (sheet-name quoting,
         the shift values' capitalization, format_range sending only the given attributes), and a second
         TestLiveFixtureParsing class (TestExcelLiveFixtureParsing) over list_worksheets.json and
         get_range.json.
    acceptance:
      - python3 -m pytest tests/unit/test_onedrive_client.py -q passes with no skips
  - id: p11-mail-client-read
    title: Outlook Mail client (reads)
    depends_on: [p05-record-fixtures]
    complexity: M
    touches:
      - src/privacyfence/outlook_mail_client.py
      - tests/unit/test_outlook_mail_client.py
    brief: |
      1. Create src/privacyfence/outlook_mail_client.py with the read half of the plan's section 3.11
         (dataclasses, static parsers, list_messages, list_conversations, get_message,
         list_conversation_messages, list_attachments, get_attachment_bytes, list_folders,
         list_categories, list_rules, resolve_folder) and section 3.6's constructor rule. Read
         tests/fixtures/live/outlook_mail/*.json first.
      2. tests/unit/test_outlook_mail_client.py: $search vs $orderby exclusivity; unread_only filter;
         conversation grouping keeps the newest per conversation and honors max_results; the
         body_text/body_html split by contentType; address rendering; resolve_folder for a well-known name,
         an id and a display name; and TestLiveFixtureParsing over all six outlook_mail fixtures with
         assert_no_placeholder_fields on the parsed message.
    acceptance:
      - python3 -m pytest tests/unit/test_outlook_mail_client.py -q passes with no skips in TestLiveFixtureParsing
  - id: p12-mail-client-write
    title: Outlook Mail client (drafts, attachments, organizing, rules)
    depends_on: [p11-mail-client-read]
    complexity: M
    touches:
      - src/privacyfence/outlook_mail_client.py
      - tests/unit/test_outlook_mail_client.py
    brief: |
      1. Add the write half of the plan's section 3.11 to src/privacyfence/outlook_mail_client.py
         (create_draft, create_reply_draft, add_attachment, move_message, archive_message,
         set_categories, create_category, create_rule, update_rule) under banners. Render Markdown with
         privacyfence.email_markdown exactly as gmail_client.py does for body_markdown. Build rule
         conditions/actions from the named fields in section 3.11's create_rule row; never emit
         forwardTo, forwardAsAttachmentTo, redirectTo, delete or permanentDelete.
      2. Extend tests/unit/test_outlook_mail_client.py: draft payload (HTML body, recipients, cc/bcc);
         reply draft creates via createReply/createReplyAll then PATCHes the body; attachment inline vs
         upload session at the 3 MiB boundary; archive uses "archive"; create_rule sequence and field
         mapping; a test asserting no request path in any write method ends with "/send"; a test that
         every generated rule action key is in the allowed set.
    acceptance:
      - python3 -m pytest tests/unit/test_outlook_mail_client.py -q passes
      - grep -rn "/send" src/privacyfence/outlook_mail_client.py finds nothing
  - id: p13-todo-connector-read
    title: To Do connector (read tools), the Microsoft website page and README row
    depends_on: [p06-todo-client, p03-family-wiring]
    complexity: M
    touches:
      - src/privacyfence/connectors/todo.py
      - tests/unit/connectors/test_todo_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/privacy_filter.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/resources/settings.yaml.example
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
      - tests/unit/test_website_connectors_page.py
      - scripts/build_site.py
      - README.md
      - website/connectors/microsoft-365/index.html
      - website/connectors/index.html
      - website/_partials/other-connectors.html
    brief: |
      1. Create src/privacyfence/connectors/todo.py with TodoConnector and the three auto tools from the
         plan's section 3.7, mirroring src/privacyfence/connectors/tasks.py's structure (_run, _fetch
         per rule 3.2.2, _auto_audit, _redact_notes with "todo_privacy"). Unknown tool → ValueError.
      2. Tables: TOOL_TO_GATE entries (auto) in auto_accept.py; "privacyfence.connectors.todo" in
         scripts/pyinstaller_common.py; TodoConnector in CONNECTOR_CLASSES
         (tests/unit/connectors/test_readme_manifest_alignment.py:36); "todo" in
         generate_tools_reference.py's CONNECTOR_TITLES/CONNECTOR_SHORT (section 3.1), then regenerate
         docs/tools-reference.md with python3 scripts/generate_tools_reference.py.
      3. The todo_privacy group (section 3.1): privacy_filter._GROUP_NAMES, settings_controller
         PRIVACY_GROUP_LABELS/PRIVACY_CATEGORY_LABELS, resources/settings.yaml.example.
      4. Website and README (section 3.1): add "microsoft-365-setup" to build_site.CONNECTOR_GUIDES; add
         "/connectors/microsoft-365/" to build_site.PAGES and an llms line next to the Jira/Confluence one
         (build_site.py:1117) that says "Outlook mail, calendar and contacts, OneDrive and Microsoft To Do
         through one Microsoft sign-in; verified on personal Microsoft accounts; no tool sends email";
         create website/connectors/microsoft-365/index.html by copying
         website/connectors/jira-confluence/index.html's structure (canonical/og URLs, the
         other-connectors include with current="microsoft-365", a "Set it up" button to the
         microsoft-365-setup guide) with content describing the five connectors, that work/school
         accounts are not yet verified, and that no tool sends email; add the page to
         website/_partials/other-connectors.html and website/connectors/index.html following the
         jira-confluence entries; add the README.md Connectors row
         "| Microsoft Outlook, OneDrive, To Do | Read mail, calendar, contacts, files and tasks; drafts,
         events, uploads, Excel edits and task changes need approval. No tool sends email. Personal
         Microsoft accounts. |"; add "todo" to test_website_connector_pages.CONNECTORS mapped to
         ("/connectors/microsoft-365/", "microsoft-365-setup", "Microsoft Outlook, OneDrive, To Do").
      5. tests/unit/connectors/test_todo_connector.py per docs/coding-and-testing-guidelines.md §2.6
         items 1, 2 and 4 (TestDispatch, one auto-tool test each proving no gate and an audit entry,
         assert_all_tools_leave_an_audit_trail), plus notes redaction under todo_privacy and the
         _fetch error mapping (unavailable_error vs RuntimeError).
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_build_site.py tests/unit/test_pyinstaller_hidden_imports.py tests/unit/test_docs_tools_reference.py tests/unit/test_privacy_filter.py tests/unit/test_systemic_gate_invariants.py -q passes
      - git diff --stat shows docs/tools-reference.md regenerated with three todo_ rows
  - id: p14-todo-connector-write
    title: To Do connector (write tools)
    depends_on: [p13-todo-connector-read]
    complexity: M
    touches:
      - src/privacyfence/connectors/todo.py
      - tests/unit/connectors/test_todo_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
    brief: |
      1. Add the five popup tools from the plan's section 3.7 to connectors/todo.py, each mirroring its
         counterpart in connectors/tasks.py (fetch existing → preview/details/preview_blocks →
         gated_call(tool=..., gate="popup", args=...) → client call). todo_move_task passes
         source_list_id and destination_list_id in args like tasks_move_task.
      2. Tables per rule 3.2.1: TOOL_TO_GATE, TOOL_TO_OPERATION ("todo.<suffix>"), TOOL_TO_VERB (the
         counterpart's verb), write_effects.EFFECT_BY_TOOL (the counterpart's sentence with "task list"
         wording kept; todo_move_task uses section 3.7's sentence), gate._TOOL_LAYOUT (the counterpart's
         value). Regenerate docs/tools-reference.md.
      3. Tests: §2.6 item 3 (each preview carries only metadata), gated_call kwargs per tool
         (gated_call_spy pattern from tests/unit/connectors/test_tasks_connector.py), the move refusal
         surfacing as RuntimeError, and assert_all_tools_leave_an_audit_trail over all eight tools.
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_auto_accept.py tests/unit/test_docs_tools_reference.py -q passes
  - id: p15-contacts-connector
    title: Outlook Contacts connector
    depends_on: [p14-todo-connector-write, p07-contacts-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/outlook_contacts.py
      - tests/unit/connectors/test_outlook_contacts_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - src/privacyfence/privacy_filter.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/resources/settings.yaml.example
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
    brief: |
      1. Create src/privacyfence/connectors/outlook_contacts.py with all seven tools of the plan's
         section 3.8, mirroring src/privacyfence/connectors/contacts.py (its auto-accepted reads, its
         update/create preview diffing, its label tools → category tools).
      2. Every table from p13 step 2 and p14 step 2 for these tools; the outlook_contacts_privacy group
         (p13 step 3's three places); "outlook_contacts" in test_website_connector_pages.CONNECTORS with
         the same page/guide/row as todo. Regenerate docs/tools-reference.md.
      3. tests/unit/connectors/test_outlook_contacts_connector.py: the full §2.6 checklist (items 1–5;
         item 5 runs tests/fixtures/live/outlook_contacts/get_contact.json through
         OutlookContactsClient._parse_contact and the update preview, with assert_no_placeholder_fields).
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_website_connector_pages.py tests/unit/test_pyinstaller_hidden_imports.py tests/unit/test_docs_tools_reference.py tests/unit/test_privacy_filter.py -q passes
  - id: p16-calendar-connector-core
    title: Outlook Calendar connector (list, read, create, update, delete)
    depends_on: [p15-contacts-connector, p08-calendar-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/outlook_calendar.py
      - tests/unit/connectors/test_outlook_calendar_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
      - tests/unit/test_connector_tool_annotations.py
    brief: |
      1. Create src/privacyfence/connectors/outlook_calendar.py with the p16 tools listed under the plan's
         section 3.9 table, mirroring src/privacyfence/connectors/calendar.py's corresponding methods
         (get_event_details passes raw_data with organizer_email and args {"calendar_id","event_id"};
         create/update/delete previews). Use section 3.9's effect sentences.
      2. Every table (as in p14 step 2), outlook_calendar_delete_event in DESTRUCTIVE_TOOLS, the
         pyinstaller/CONNECTOR_CLASSES/CONNECTOR_TITLES/CONNECTOR_SHORT/website-mapping entries.
         Regenerate docs/tools-reference.md.
      3. tests/unit/connectors/test_outlook_calendar_connector.py: the full §2.6 checklist (item 5 over
         get_event.json through _parse_event and the get_event_details preview).
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_connector_tool_annotations.py tests/unit/test_website_connector_pages.py tests/unit/test_docs_tools_reference.py -q passes
  - id: p17-calendar-connector-extras
    title: Outlook Calendar connector (visibility, categories, out of office)
    depends_on: [p16-calendar-connector-core]
    complexity: M
    touches:
      - src/privacyfence/connectors/outlook_calendar.py
      - tests/unit/connectors/test_outlook_calendar_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
    brief: |
      1. Add the p17 tools from the plan's section 3.9 to connectors/outlook_calendar.py, mirroring
         calendar_get_event_visibility, calendar_list_colors, calendar_create_out_of_office,
         calendar_set_event_visibility and calendar_set_event_color. The visibility value is validated
         before the gate (anything but normal/private raises ValueError with the allowed values).
      2. Tables as in p14 step 2; section 3.9's out-of-office effect sentence. Regenerate
         docs/tools-reference.md.
      3. Tests: previews metadata-only, the invalid-visibility rejection never reaching gated_call, the
         out-of-office call carrying no calendar_id in args, audit sweep over all eleven tools.
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_docs_tools_reference.py -q passes
  - id: p18-onedrive-connector-read
    title: OneDrive connector (list, metadata, content, download)
    depends_on: [p17-calendar-connector-extras, p09-onedrive-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/onedrive.py
      - tests/unit/connectors/test_onedrive_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - src/privacyfence/privacy_filter.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/resources/settings.yaml.example
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
    brief: |
      1. Create src/privacyfence/connectors/onedrive.py with onedrive_list_files, onedrive_list_folder,
         onedrive_get_file_metadata, onedrive_get_file_content and onedrive_download_file from the plan's
         section 3.10, mirroring the drive_* counterparts in src/privacyfence/connectors/drive.py
         (content extraction and its size cap, PII scan text, download_mode delivery including the org
         inline/staged-link branch and local_files.deliver_file, the delivery= kwarg). raw_data for
         the gated reads is {"file": item} so auto_accept._file_from finds it (its parent_id is what
         p26's folder scopes read).
      2. Tables (as in p14 step 2), the onedrive_privacy group (p13 step 3's three places), the
         pyinstaller/CONNECTOR_CLASSES/titles/website-mapping entries. Regenerate docs/tools-reference.md.
      3. tests/unit/connectors/test_onedrive_connector.py: the full §2.6 checklist (item 5 over
         get_file_metadata.json), plus download delivery in local and org mode (inline and staged link)
         mirroring tests/unit/connectors/test_drive_connector.py's download tests.
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_website_connector_pages.py tests/unit/test_privacy_filter.py tests/unit/test_docs_tools_reference.py -q passes
  - id: p19-onedrive-connector-write
    title: OneDrive connector (upload, move, write text content)
    depends_on: [p18-onedrive-connector-read]
    complexity: M
    touches:
      - src/privacyfence/connectors/onedrive.py
      - tests/unit/connectors/test_onedrive_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
    brief: |
      1. Add onedrive_upload_file, onedrive_move_file and onedrive_write_file_content (section 3.10),
         copying drive_upload_file's file-source handling and ADR 0102 slot consumption
         (connectors/drive.py:1290-1460), drive_move_file's preview (args carry file_id and
         destination_folder_id; raw_data {"file": item}), and drive_write_file_content's preview. The
         text-only check for onedrive_write_file_content runs before the gate.
      2. Tables as in p14 step 2. Regenerate docs/tools-reference.md.
      3. Tests: each file source (local_path, content_base64, upload_id; exactly-one rule), the slot is
         consumed only after approval, the non-text refusal never reaches gated_call, previews
         metadata-only, audit sweep over all eight tools.
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_docs_tools_reference.py -q passes
  - id: p20-onedrive-excel-connector
    title: OneDrive connector (Excel tools)
    depends_on: [p19-onedrive-connector-write, p10-onedrive-excel-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/onedrive.py
      - tests/unit/connectors/test_onedrive_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - tests/unit/test_connector_tool_annotations.py
    brief: |
      1. Add the eight onedrive_excel_* tools from the plan's section 3.10, each mirroring its
         drive_sheets_* counterpart in connectors/drive.py (range parsing "Sheet!A1:B2" into sheet and
         address; values JSON validation before the gate; the .xlsx-only refusal before the gate).
         raw_data {"file": item} for every gated one. onedrive_excel_delete_dimensions is destructive.
      2. Tables as in p14 step 2, DESTRUCTIVE_TOOLS. Regenerate docs/tools-reference.md.
      3. Tests: range parsing (with and without a sheet, quoted sheet names), the .xlsx refusal, values
         validation, previews metadata-only, §2.6 item 5 over tests/fixtures/live/onedrive_excel/get_range.json
         through _parse_range_values and the get_values preview, audit sweep over all sixteen tools.
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_connector_tool_annotations.py tests/unit/test_docs_tools_reference.py -q passes
  - id: p21-mail-connector-read
    title: Outlook Mail connector (lists, message, conversation, attachment download)
    depends_on: [p20-onedrive-excel-connector, p11-mail-client-read]
    complexity: M
    touches:
      - src/privacyfence/connectors/outlook_mail.py
      - tests/unit/connectors/test_outlook_mail_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - src/privacyfence/privacy_filter.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/resources/settings.yaml.example
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
    brief: |
      1. Create src/privacyfence/connectors/outlook_mail.py with the p21 tools from the plan's section
         3.11, mirroring connectors/gmail.py: _get_message (gmail.py:699 onward: html_to_text, apply_text
         with "outlook_mail_privacy" categories body/metadata/attachments, pii_scan_text,
         include_html, visibility, content_kind="email"), get_conversation mirroring get_thread
         (gmail.py:822-827, thread_history category), download_attachment mirroring
         gmail_download_attachment (gmail.py:881-1082: delivery estimate, prefetch for preview,
         org/local delivery), and the six auto list tools. raw_data for gated reads is the parsed
         OutlookMessage (its sender feeds p26's sender-domain scope).
      2. Tables (as in p14 step 2), the outlook_mail_privacy group (section 3.1), the
         pyinstaller/CONNECTOR_CLASSES/titles/website-mapping entries. Regenerate docs/tools-reference.md.
      3. tests/unit/connectors/test_outlook_mail_connector.py: the full §2.6 checklist (item 5 over
         get_message.json through _parse_message and the get_message preview), mirroring
         tests/unit/connectors/test_gmail_connector.py's TestGetMessagePreviewMinimization and download
         tests.
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_website_connector_pages.py tests/unit/test_privacy_filter.py tests/unit/test_docs_tools_reference.py -q passes
  - id: p22-mail-connector-drafts
    title: Outlook Mail connector (six draft tools)
    depends_on: [p21-mail-connector-read, p12-mail-client-write]
    complexity: M
    touches:
      - src/privacyfence/connectors/outlook_mail.py
      - tests/unit/connectors/test_outlook_mail_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
    brief: |
      1. Add the six draft tools from the plan's section 3.11, mirroring the six gmail draft tools in
         connectors/gmail.py (the card shows the raw Markdown; reply-all lists every participant; the
         *_with_attachments variants take the same file sources and upload-slot handling as the gmail
         ones). The client's add_attachment is called once per file after the draft exists.
      2. Tables as in p14 step 2 (all six share op outlook_mail.create_draft). Regenerate docs/tools-reference.md.
      3. Tests: previews metadata-only, the reply-all participant list, attachments added after the draft
         is created, slot consumption after approval, audit sweep over all fifteen tools.
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_docs_tools_reference.py -q passes
  - id: p23-mail-connector-organize
    title: Outlook Mail connector (categories, archive, move, rules)
    depends_on: [p22-mail-connector-drafts]
    complexity: M
    touches:
      - src/privacyfence/connectors/outlook_mail.py
      - tests/unit/connectors/test_outlook_mail_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
    brief: |
      1. Add outlook_mail_add_category, _remove_category, _create_category, _archive_message,
         _move_message, _create_rule and _update_rule from the plan's section 3.11, mirroring the gmail
         label/archive/filter tools. outlook_mail_move_message has no counterpart: use the row's verb,
         layout, effect and preview exactly. Rule tools validate that at least one criterion and one action
         are given before the gate, as gmail_create_filter does.
      2. Tables as in p14 step 2. Regenerate docs/tools-reference.md.
      3. Tests: previews metadata-only; the rule tools expose no forward_to param (assert on tool_specs);
         the validation errors never reach gated_call; audit sweep over all twenty-two tools.
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_docs_tools_reference.py -q passes
      - python3 -c "from privacyfence.connectors.outlook_mail import OutlookMailConnector as C; from unittest.mock import MagicMock; print(len(C(MagicMock()).tool_specs()))" prints 22
  - id: p24-daemon-wiring
    title: Build the five connectors in the daemon, list them in Settings and on the connect page
    depends_on: [p23-mail-connector-organize, p03-family-wiring]
    complexity: M
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_connect.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_routes_connect.py
    brief: |
      1. daemon_main.build_connectors: the family load and the five blocks exactly as the plan's section
         3.6 (p24 bullets) says, mirroring the Atlassian load (:1455-1466), the Jira/Confluence blocks
         (:1468-1500) and the Gmail/Drive download settings (:1279-1312).
      2. settings_controller and routes_connect: fill ALL_CONNECTORS, _CONNECTOR_LABEL_OVERRIDES,
         ORG_CONFIG_SERVICE, MICROSOFT_CONNECTORS, MICROSOFT_SERVICES and SERVICE_LABELS per section 3.6.
         Remove the p03 tests' monkeypatching of MICROSOFT_CONNECTORS/MICROSOFT_SERVICES now that they are real.
      3. Tests: tests/unit/test_daemon_main.py TestBuildConnectorsMicrosoft (mirroring
         TestBuildConnectorsAtlassian :989): no org section → failures "no_org_config" for all five; no
         token → "not_authenticated"; a disabled connector is skipped; success builds each with my_email;
         mail/onedrive carry download settings. Extend TestBuildConnectorsFailureReasons. Settings: the five
         rows render with labels, has_org from the microsoft section. Connect page: five rows sharing the
         microsoft grant.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/web -q passes
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q passes
  - id: p25-policy-todo-calendar-contacts
    title: Always-allow scopes and grants for To Do, Outlook Calendar and Outlook Contacts
    depends_on: [p23-mail-connector-organize]
    complexity: M
    worker_model: opus
    worker_model_reason: The policy layer's selector, proposal, catalogue and grant tables are held together by cross-cutting invariants (every proposal must accept its own item, frozen v1 references, grant capabilities reproduced exactly) that a new scope must satisfy by reading how existing ones do; this needs judgement the brief cannot fully mechanize.
    touches:
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/resource_registry.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_propose.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_resource_registry.py
      - docs/always-allow-rules-reference.md
    brief: |
      1. Add approved_todo_list, approved_outlook_calendar, i_am_outlook_organizer and
         outlook_contacts_category_allowlist as NEW_SCOPE_SELECTORS entries, with PROPOSABLE_SCOPES
         entries in the Tasks/Calendar/Contacts style, exactly per the plan's section 3.12 table; give
         outlook_contacts.edit the same condition-scope treatment contacts.edit has; add VALUE_HINTS; add
         the todo.task_lists and outlook_calendar.calendars GRANT_RESOURCE_TYPES mirroring the tasks and
         calendar entries.
      2. Tests: each new selector's matches() directly (positive, negative, spoofed identity for the
         fetched one, as _SPOOF_IDENTITY_SELECTORS does), proposals for one representative tool per
         connector (each accepts its own item; identity before attribute), grant capabilities reproduced
         exactly, catalogue coverage.
      3. python3 scripts/generate_always_allow_reference.py and commit docs/always-allow-rules-reference.md.
      Stop with status=blocked if satisfying a test would require editing SCOPE_SELECTORS or
      tests/unit/policy/_v1_reference.py.
    acceptance:
      - python3 -m pytest tests/unit/policy tests/unit/test_auto_accept.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_gate.py -q passes
      - git diff --name-only shows tests/unit/policy/_v1_reference.py unchanged
  - id: p26-policy-mail-onedrive
    title: Always-allow scopes and grants for Outlook Mail and OneDrive
    depends_on: [p25-policy-todo-calendar-contacts]
    complexity: M
    worker_model: opus
    worker_model_reason: Same as p25, plus the four OneDrive folder predicates must reproduce Drive's args-vs-fetched split and move semantics without copying their v1-frozen entries.
    touches:
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/resource_registry.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_propose.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_resource_registry.py
      - docs/always-allow-rules-reference.md
    brief: |
      1. Add trusted_outlook_sender_domain, outlook_mail_category_allowlist, the outlook_mail.anything
         always_allow entry, the outlook_mail.anything EXTRA_SCOPES entry for create_rule/update_rule
         (mirroring gmail.create_filter/update_filter), and the four OneDrive folder predicates, exactly per
         the plan's section 3.12; VALUE_HINTS; the onedrive.folders GRANT_RESOURCE_TYPE mirroring drive's.
      2. Tests as in p25 step 2 (spoofed identity for the fetched ones; a move proposal requires both ends;
         create_rule/update_rule are never proposed from a popup).
      3. Regenerate docs/always-allow-rules-reference.md.
      Stop with status=blocked if satisfying a test would require editing SCOPE_SELECTORS or
      tests/unit/policy/_v1_reference.py.
    acceptance:
      - python3 -m pytest tests/unit/policy tests/unit/test_auto_accept.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_gate.py -q passes
      - git diff --name-only shows tests/unit/policy/_v1_reference.py unchanged
  - id: p27-recorder-lifecycle
    title: Recorder --lifecycle for To Do and Outlook Calendar
    depends_on: [p06-todo-client, p08-calendar-client]
    complexity: S
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - docs/connector-qa.md
    brief: |
      1. Add lifecycle_todo and lifecycle_outlook_calendar to scripts/qa_fixture_recorder.py and
         LIFECYCLE_CHECKS, mirroring lifecycle_tasks (:2046-2109) and lifecycle_calendar: create a
         "[QATEST-LIFECYCLE] <uuid8>" task in the QA list (resolved by list_name) / event on the QA calendar
         (resolved by calendar_name, dated inside window_start..window_end), read it back, update it,
         read it back, delete it in finally, and confirm deletion with _confirm_deleted where a 404 on
         re-fetch counts as deleted. Build clients with TodoClient/OutlookCalendarClient(org_section,
         token_path) from the family config.
      2. Update the comment above LIFECYCLE_CHECKS and docs/connector-qa.md's Manifest reference
         "--lifecycle" column for todo and outlook_calendar.
      3. Tests mirroring TestLifecycleTasks with fake clients: success, a failed read-back, a cleanup that
         leaves the object (cleanup_ok False).
    acceptance:
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q passes
  - id: p28-retire
    title: ADRs, reference docs, changelog, one live check, and delete the plan
    depends_on: [p24-daemon-wiring, p26-policy-mail-onedrive, p27-recorder-lifecycle, p12-mail-client-write]
    complexity: M
    touches:
      - docs/adr/0114-microsoft-connectors-share-one-grant-and-never-request-mail-send.md
      - docs/adr/0115-the-microsoft-client-id-comes-from-the-organization-config.md
      - docs/adr/0116-microsoft-graph-is-called-through-one-requests-based-transport.md
      - docs/adr/0117-loopback-sign-in-is-a-public-client-org-mode-is-confidential.md
      - docs/adr/0118-graph-gaps-are-approximated-in-the-open.md
      - docs/adr/0119-microsoft-fixtures-are-recorded-from-raw-graph-reads-before-the-clients.md
      - docs/adr/README.md
      - docs/microsoft-365-setup.md
      - docs/getting-started.md
      - docs/connecting-a-service.md
      - docs/configuration-reference.md
      - docs/approvals-and-policy.md
      - CHANGELOG.md
      - docs/microsoft-connectors-plan.md
      - docs/microsoft-connectors-plan-manual-steps.html
    brief: |
      1. Write ADRs 0114–0119 from the plan's section 4 with docs/adr/README.md's template (Status
      Accepted with today's date; Context from sections 2–3; Decision; Rejected alternatives; Consequences),
      linking source files, ADR 0040/0017/0102/0106 where relevant and
      https://github.com/privacyfence/privacyfence/issues/828 for 0115, never the plan. Take the next free
      numbers on origin/main at the time (rename files if main took any). Add them to the index.
      2. docs/microsoft-365-setup.md: add "What each connector can do" (link docs/tools-reference.md's
      sections), "What is not available" (Word editing, file comments, forwarding rules, free/busy, rooms,
      working location, shared drives, Teams; the last five with #828's URL), and the privacy groups.
      Link the guide from docs/getting-started.md and docs/connecting-a-service.md next to the Atlassian
      guide; add the four privacy groups to docs/configuration-reference.md; add the Microsoft scopes to
      docs/approvals-and-policy.md where the Google scopes are listed (only if such a list exists).
      3. CHANGELOG.md, under ## [Unreleased]: one Added entry for the five connectors (verified on personal
      Microsoft accounts; work/school accounts tracked in #828's URL), no version heading.
      4. Delete docs/microsoft-connectors-plan.md and docs/microsoft-connectors-plan-manual-steps.html;
      grep -rn "microsoft-connectors-plan" . --exclude-dir=.git must find nothing.
      5. Dispatch .github/workflows/connector-live-check.yml against this phase branch (GitHub MCP
      actions_run_trigger, no inputs), wait for it with the Monitor tool or send_later check-ins, and put
      the run URL and its report's Microsoft rows in your PHASE-REPORT. If it opens a drift PR, do not
      follow it; report its URL. A failing Microsoft check or lifecycle stops this phase with
      status=blocked quoting the failing rows.
      6. Run the full /dod.
    acceptance:
      - ls docs/adr | grep -cE "^011[4-9]-" is 6 (or the renumbered equivalents), each with "Accepted"
      - grep -rn "microsoft-connectors-plan" . --exclude-dir=.git finds nothing
      - grep -n "Unreleased" -A30 CHANGELOG.md shows the Microsoft entry
      - the PHASE-REPORT links a completed connector-live-check.yml run whose Microsoft --check and --lifecycle rows pass
```
