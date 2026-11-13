# Credential provisioning to agent runtimes, n8n first

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An administrator sets `PRIVACYFENCE_N8N_URL` and `PRIVACYFENCE_N8N_API_KEY` on an organization server and turns on **Admin → Agent runtimes**. Claude calls `privacyfence_propose_scoped_token` with `deliver_to: {"runtime": "n8n", "workflow_id": "..."}` and hands the person the review link. The person narrows and confirms the token in Settings. PrivacyFence then writes the token into n8n as an `httpBearerAuth` credential; the browser never sees the secret. Claude calls `privacyfence_runtime_credential_status` to get the credential id, and puts it on the n8n MCP Client node. Revoking the token, or letting it expire, deletes the n8n credential within five minutes.

**Scope:** New modules for the runtime config, the n8n API client, delivery records and a cleanup sweeper. One new meta tool, an optional `deliver_to` argument on the propose tool, an Admin page section, a delivery branch on the organization "create from proposal" route, and Settings rows and review text. Organization mode only, and only through an AI proposal. The scoped token model itself (deny-only, no cards, `scoped-token:<id>` in the audit log) does not change; it already makes an n8n run fail fast on any call that would need an approval.

**Security:** The secret now leaves PrivacyFence and is stored in n8n. Anyone who can edit workflows there, the proposing AI client included, can use and read it, so narrow scope and expiry are the protection and the review page says so (§3.1). The n8n address and key come only from the server environment, never from a web page (§3.2). The key is never logged, audited or returned. Every delivery, deletion and failure is audited.

**Review focus:** the two-phase write that keeps a live token from ever lacking a record (§3.5); the sweeper's rules for what it may delete and when (§3.7); what the status tool reveals and to whom (§3.6); the ADR 0250 and ADR 0165 amendments (§4).

## 1. Goal

Claude builds n8n workflows through the n8n MCP and wants those workflows to call PrivacyFence's `/mcp`. The MCP Client node needs a bearer credential that Claude may not write. This change lets PrivacyFence broker that credential with a person's approval: Claude proposes a narrow scoped token, the person confirms it, PrivacyFence creates the n8n credential and tells Claude its id, and PrivacyFence removes the credential again when the token is revoked or expires. It is built as "credential provisioning to agent runtimes" with n8n as the first adapter, so a later runtime reuses the proposal, approval, expiry, revocation, records and sweeper, and adds only a delivery client. Tracking issue: https://github.com/privacyfence/privacyfence/issues/1090.

## 2. Current state

- **Scoped tokens** (`src/privacyfence/scoped_tokens.py`, ADR 0164, 0165, 0236). `ScopedTokenStore.create` (`scoped_tokens.py:595`) picks the token id itself; `revoke` (`:747`) and `revoke_all` (`:780`) return a bool and a count and call nothing. `get` (`:922`) returns `None` for a principal the store has not loaded, and `_reload` (`:542`) turns an unreadable file into "no tokens". Expired tokens stay in the file; nothing prunes them.
- **The audit identity of a token** is `scoped-token:<id>` named by its label (`web/routes_mcp.py:113`). A scoped call that no token rule covers is denied at once as `denied_scoped_token` (`gate.py` `_deny_scoped`), with no card and no pending approval. Fail-fast is therefore already the behaviour; no Wait-node design is needed.
- **Proposals** (`src/privacyfence/scoped_token_proposals.py`, ADR 0250). `TokenProposal` (`:40`) is frozen and in memory for one hour. `McpDispatcher.propose_scoped_token` (`web/mcp_dispatch.py:526`) returns a review link and says the AI "will not learn whether it was created" (`:590`).
- **Organization create-from-proposal** (`web/routes_settings.py:2036`) takes the proposal, then `_create_and_deliver` (`:1971`) mints the token and returns the secret once in JSON (`:2004`). The step-up fingerprint binds the request and `proposal_id` (`:2017`). Revoke routes are at `:2081` and `:2118`. The local-mode equivalents are at `:1284` and `:1343`.
- **Meta tools**: `META_TOOLS` in `web/mcp_tools.py:350`; listing filter in `web/routes_mcp.py` `handle_list_tools` (about `:455`); dispatch in `_dispatch_meta_tool` (about `:601`); scoped tokens see only `SCOPED_META_TOOLS` (`web/routes_mcp.py:107`). `scripts/generate_tools_reference.py:211` hard-codes "eight always, plus `privacyfence_propose_scoped_token`".
- **External keys** are read per use from a variable or its `_FILE` twin by `org_mode.provisioning_key` (`org_mode.py:175`). Peek and Grist send their keys only to addresses from the environment or the signed import (`admin_config.py:478-486`).
- **Admin config** (`admin_config.py`, ADR 0257): sections in `_default_config` (`:134`) and `OPERATIONAL_SECTIONS` (`:67`); actions in `_ACTION_MODES` (`:450`) and `apply_change` (`:909`); `web/routes_admin.py` has `ADMIN_ACTIONS` (`:108`), `ADMIN_PARAMS` (`:132`), `RESTART_LABELS` (`:169`) and `_ORG_STATE_KEYS` (`:203`). The Admin page forms are in `settings_window_html.py` (`ADMIN_NAV_ITEMS` `:2064`, `ADMIN_FORMS` `:2388`, `renderTokens` `:2523`, defaults `:3357`).
- **Worker pattern**: `peek_provisioning.py` runs one thread started from `daemon_main.py:1459` with `stop_event=_shutdown_event`.
- **The n8n side** (research r-20261113-1, read 2026-11-13, quoted in §3.3). The connected n8n instance reports `teamProjectsEnabled: false`, so it has no team projects.

## 3. Design

### 3.1 The model

- A delivered credential **is a scoped token**. No new principal object is added: the token already is the "workflow X, running unattended" identity, it is deny-only, expires, is revoked alone, and is named in every audit entry as `scoped-token:<id>`. The person who approved it appears only in the creation and delivery entries.
- **Delivery is an adapter.** `agent_runtimes.py` holds what is runtime-neutral (the `deliver_to` request, the target, the config); `n8n_api.py` is the only n8n-specific code. The records file and the sweeper carry a `runtime` field (`"n8n"` is the only value).
- **Organization mode only**, including a single-user organization install. A proposal with `deliver_to` is refused in local mode.
- **Only through an AI proposal.** The New token form gets no delivery choice. The delivery target lives on the `TokenProposal`; the browser request can never set or change it.
- **The workflow id is advisory.** `deliver_to.workflow_id` is what the AI client reports. It is shown on the review page, written into the record and the audit log, and never enforced: any workflow that can reach the credential can use it.
- **The secret is readable through n8n.** An `httpBearerAuth` credential can be attached to any node of any workflow owned by the n8n user that holds it, including an HTTP Request node pointed at an echo endpoint, so the proposing client can read the token through n8n's execution data. This amends ADR 0250's "the client never receives the token" for delivered proposals, and ADR 0165's "never stored" (n8n stores it, encrypted under its own key). The review page, the confirmation dialog, the tool texts and the docs say so.
- **The same gates as any token creation.** Confirming a delivery uses the existing route gates: signed-in session, CSRF, origin, typed name, and step-up where the install requires it. A passkey is not forced on top (rejected in §3.10).

### 3.2 Configuration

Admin config section, added to `admin_config._default_config()` and to `OPERATIONAL_SECTIONS`:

```json
"agent_runtimes": {"n8n": {"enabled": false, "project_id": "", "mcp_url": ""}}
```

Server environment, read only by `agent_runtimes.py`:

| Variable | Meaning |
|---|---|
| `PRIVACYFENCE_N8N_URL` | The n8n base URL, for example `https://n8n.internal.example`. https only; private hosts allowed; refused: userinfo, query, fragment, an IP literal that is link-local (`169.254.0.0/16`, `fe80::/10`), unspecified (`0.0.0.0`, `::`) or multicast, and the host `metadata.google.internal`. A path is kept with its trailing `/` removed. |
| `PRIVACYFENCE_N8N_API_KEY` or `PRIVACYFENCE_N8N_API_KEY_FILE` | The n8n API key, read per use with `org_mode.provisioning_key("PRIVACYFENCE_N8N_API_KEY")`. Never written to any file PrivacyFence owns, never in a log line, an audit entry, an error text or a response. |

- `enabled` turns delivery on. `project_id` (empty, or `[A-Za-z0-9]{1,36}`) is the n8n project the credential is created in; empty means the API key owner's personal space. `mcp_url` (empty, or an `https://` URL on any host, or `http://` on `localhost`, `127.0.0.1` or `::1`) overrides the PrivacyFence `/mcp` URL that Claude is told to use; empty means the base URL of the propose call plus `/mcp`.
- The Admin page edits only `enabled`, `project_id` and `mcp_url` (action `set_agent_runtimes`, sensitive, `restart`, organization only). It shows the URL and whether the key is set read-only, with the two variable names. Changing the address therefore needs the server's environment, so taking over an admin session cannot send the key to another host.
- **Delivery is available** when `enabled` is true, `PRIVACYFENCE_N8N_URL` is set and valid, the key is set, scoped tokens are enabled and AI proposals are allowed. A malformed `agent_runtimes` section, or a malformed `PRIVACYFENCE_N8N_URL`, refuses startup with a `ConfigurationError`. A missing URL or key only makes delivery unavailable, with one warning at startup.
- Minimum n8n version: 2.40.0 (the first with `projectId` on POST /credentials). It is documented, not checked.

### 3.3 The n8n client (`src/privacyfence/n8n_api.py`)

The facts the client relies on, from research r-20261113-1 (read 2026-11-13):

- POST `/api/v1/credentials` (scope `credential:create`) takes `name`, `type` and `data`, optional `id` (1-16 characters) and `projectId` ("Defaults to the user's personal project", from 2.40.0). It returns 200 with `id`, `name`, `type` and timestamps, never secrets. An `Idempotency-Key` header is accepted. Source: https://cdn.jsdelivr.net/npm/n8n@2.47.7/dist/public-api/v1/openapi.yml.
- `httpBearerAuth` has one data field, `token`, which n8n sends as `Authorization: Bearer {{token}}`. Source: https://cdn.jsdelivr.net/npm/n8n-nodes-base@latest/dist/credentials/HttpBearerAuth.credentials.js.
- DELETE `/api/v1/credentials/{id}` (scope `credential:delete`; the caller must own the credential) returns 200 with the deleted record. Source: the same openapi.yml and https://docs.n8n.io/connect/n8n-api/credential.
- `GET /api/v1/credentials/schema/{type}` declares no required scope.
- Scopes are Enterprise-only: other keys "have full access". A key can be limited to `credential:create` and `credential:delete`. Source: https://docs.n8n.io/api/authentication/.
- The n8n MCP server's `list_credentials` returns metadata only, and no n8n MCP tool creates a credential or reads a secret. Source: https://docs.n8n.io/connect/connect-to-n8n-mcp-server/mcp-server-tools-reference/.

The interface:

```python
class N8nApiError(Exception):
    """A fixed text naming the host and at most an HTTP status; never a body, header or request."""
    def __init__(self, message: str, *, status: int | None = None) -> None: ...

class N8nApi:
    def __init__(self, base_url: str, key: str) -> None: ...
    def create_bearer_credential(self, *, credential_id: str, name: str, token: str, project_id: str) -> None: ...
    def delete_credential(self, credential_id: str) -> bool: ...   # True deleted, False already gone (404)
    def check(self) -> None: ...                                     # GET /api/v1/credentials/schema/httpBearerAuth
```

- `requests.Session`, headers `X-N8N-API-KEY: <key>` and `Accept: application/json`. Every call passes `timeout=REQUEST_TIMEOUT_SECONDS` (30) and `allow_redirects=False`. `__repr__` shows the host only. TLS verification is never turned off; an internal CA is trusted through `REQUESTS_CA_BUNDLE` (documented).
- `create_bearer_credential` posts `{"id": credential_id, "name": name, "type": "httpBearerAuth", "data": {"token": token}}`, plus `"projectId": project_id` when it is not empty, with the header `Idempotency-Key: <credential_id>`. Success is a 200 or 201 whose JSON has `id == credential_id` and `type == "httpBearerAuth"`. When the JSON names a different id that matches `^[A-Za-z0-9]{1,16}$`, the client deletes that id once (best effort, errors swallowed) and then raises.
- `delete_credential` refuses an id that does not match `^[0-9a-f]{16}$` with `ValueError`. 200 or 204 returns `True`, 404 returns `False`, and anything else raises.
- A response body over 1,000,000 bytes is an error. A 3xx raises an error naming `PRIVACYFENCE_N8N_URL`.
- The exact error texts (`<host>` is the URL's host):
  - `"n8n at <host> could not be reached."` for a `requests.RequestException`.
  - `"n8n at <host> answered HTTP <status>."` for any other status.
  - `"n8n at <host> redirected the request; set PRIVACYFENCE_N8N_URL to the address it redirects to."` for a 3xx.
  - `"n8n at <host> did not create the credential as asked."` for a wrong id or type, or an unreadable answer.
- It is named `n8n_api.py`, not `*_client.py`, because n8n is not a connector and the §2.7 connector-QA row must not apply (compare `grist_provisioner_api.py`).

### 3.4 Runtime config and request types (`src/privacyfence/agent_runtimes.py`)

```python
N8N_URL_ENV = "PRIVACYFENCE_N8N_URL"
N8N_API_KEY_ENV = "PRIVACYFENCE_N8N_API_KEY"  # the variable's name, never a key; secret-scan:allow
RUNTIMES = ("n8n",)

@dataclass(frozen=True)
class N8nRuntimeConfig:
    enabled: bool
    base_url: str        # normalized PRIVACYFENCE_N8N_URL, "" when unset
    project_id: str
    mcp_url: str
    @classmethod
    def from_org_config(cls, org_config: dict, *, environ: Mapping[str, str] | None = None) -> "N8nRuntimeConfig": ...

@dataclass(frozen=True)
class DeliveryTarget:
    runtime: str         # "n8n"
    workflow_id: str     # "" when the client gave none

def normalize_n8n_url(raw: str) -> str: ...                 # raises org_mode.ConfigurationError
def endpoint(url: str) -> tuple[str, str, int]: ...         # (scheme, lower-case host, port; 443/80 by default)
def api_key() -> str | None: ...                            # org_mode.provisioning_key(N8N_API_KEY_ENV)
def delivery_available(config: N8nRuntimeConfig | None) -> bool: ...   # config.enabled and base_url and api_key()
def parse_deliver_to(raw: object) -> DeliveryTarget: ...    # raises ValueError
def credential_name(label: str) -> str: ...                 # f"PrivacyFence {label}"
def mcp_url_for(config: N8nRuntimeConfig, base_url: str) -> str: ...  # config.mcp_url or base_url.rstrip("/") + "/mcp"
def api_for(config: N8nRuntimeConfig) -> N8nApi | None: ... # None unless base_url and api_key() are set
```

- `from_org_config` reads `org_config.get("agent_runtimes", {}).get("n8n", {})`. A non-object section, a non-bool `enabled`, a bad `project_id` or a bad `mcp_url` raises `org_mode.ConfigurationError`; so does a set but malformed `PRIVACYFENCE_N8N_URL`. `environ` defaults to `os.environ`.
- `parse_deliver_to` accepts exactly a dict whose keys are a subset of `{"runtime", "workflow_id"}`, with `runtime == "n8n"` and `workflow_id` absent, empty or matching `^[A-Za-z0-9]{1,36}$`. Its error texts:
  - not a dict: `"deliver_to is {runtime: 'n8n', workflow_id}."`
  - an unknown key: `"deliver_to takes only runtime and workflow_id."`
  - another runtime: `"The only runtime PrivacyFence delivers to is n8n."`
  - a bad workflow id: `"workflow_id is the n8n workflow's id: 1 to 36 letters and digits."`

### 3.5 Delivery records and the two-phase write (`src/privacyfence/runtime_credentials.py`)

The file is `paths.org_dir() / "agent_runtime_credentials.json"`, written as `{"version": 1, "records": [...]}` with `secure_files.atomic_write_json`. Only the daemon writes it.

```python
CREATING, DELIVERED, ABANDONED = "creating", "delivered", "abandoned"
BOOT_ID: str   # secrets.token_hex(8), fixed for the process

@dataclass(frozen=True)
class RuntimeCredential:
    principal_id: str
    token_id: str
    proposal_id: str
    runtime: str                      # "n8n"
    endpoint: tuple[str, str, int]    # agent_runtimes.endpoint(base_url) at delivery
    project_id: str
    credential_id: str                # secrets.token_hex(8)
    credential_name: str
    workflow_id: str
    mcp_url: str
    state: str
    created_at: float
    boot_id: str

class RuntimeCredentialStoreError(RuntimeError): ...

class RuntimeCredentialStore:
    def __init__(self, path: Path) -> None: ...
    def add(self, record: RuntimeCredential) -> None: ...
    def set_state(self, credential_id: str, expected: str, new: str) -> bool: ...   # compare-and-set
    def remove(self, credential_id: str) -> None: ...
    def all(self) -> list[RuntimeCredential]: ...
    def for_token(self, principal_id: str, token_id: str) -> RuntimeCredential | None: ...
    def for_proposal(self, principal_id: str, proposal_id: str) -> RuntimeCredential | None: ...
    def in_flight(self, credential_id: str) -> contextlib.AbstractContextManager[None]: ...
    def is_in_flight(self, credential_id: str) -> bool: ...

def get_store() -> RuntimeCredentialStore: ...
def set_store(store: RuntimeCredentialStore | None) -> None: ...
```

- **File rules.** One `threading.Lock` guards every read-modify-write and the in-flight set. A file that cannot be parsed is never rewritten: `all()` and the lookups return what parsed (nothing for an unreadable file) and log one warning, and `add`, `set_state` and `remove` raise `RuntimeCredentialStoreError`. The secret is never in a record.
- **`deliver`**, the whole request-path flow, with no web imports:

  ```python
  @dataclass(frozen=True)
  class DeliveryOutcome:
      token: ScopedToken | None
      record: RuntimeCredential | None
      error: str          # "" on success, else one of the texts below
      status: int         # 200, 400, 500 or 502

  def deliver(*, principal: Principal, bound: dict, proposal: TokenProposal, config: N8nRuntimeConfig,
              api: N8nApi, token_store: ScopedTokenStore, policy: ScopedTokenPolicy) -> DeliveryOutcome: ...
  ```

  1. Pick `token_id = scoped_tokens.new_token_id()` and `credential_id = secrets.token_hex(8)`.
  2. Write the record in state `creating`, with `boot_id=BOOT_ID`, inside `in_flight(credential_id)`. Steps 2-5 all run inside it.
  3. `token_store.create(principal, ..., token_id=token_id)`. A `ScopedTokenError` removes the record and returns status 400 with its text.
  4. `api.create_bearer_credential(credential_id=..., name=credential_name(label), token=secret, project_id=config.project_id)`.
  5. On success, `set_state(credential_id, CREATING, DELIVERED)`. If that returns `False` (the record vanished), revoke the token, delete the credential (best effort) and fail as in 6.
  6. On `N8nApiError` (or any exception) in 4: revoke the token first. If the revoke succeeds, set the record to `abandoned`, wake the sweeper and return 502 with `DELIVERY_FAILED`. If the revoke raises, leave the record as it is and return 500 with `DELIVERY_FAILED_NOT_REVOKED`.

  The texts:

  ```python
  DELIVERY_FAILED = ("PrivacyFence could not create the credential in n8n, so it revoked the token. "
                     "Try again, or ask your administrator to check the n8n connection.")
  DELIVERY_FAILED_NOT_REVOKED = ("PrivacyFence could not create the credential in n8n and could not revoke "
                                 "the token. Revoke it now in Settings → Scoped tokens.")
  ```

- **`status_for(principal_id, proposal_id, *, token_store, registry, now) -> dict`** builds the status tool's answer (§3.6).
- **Audit** (connector `policy`, tool `privacyfence_runtime_credential`, written under `principal_scope` of the record's principal; summaries carry ids, the host and names, never a secret or key; text from the AI client has its control characters removed):
  - `runtime_credential_delivered`: `"Delivered scoped token '<label>' (<token id>) to n8n at <host> as credential '<name>' (<credential id>)[, for workflow <id> as reported by the AI client]"`.
  - `runtime_credential_delivery_failed`: `"Could not deliver scoped token '<label>' (<token id>) to n8n at <host>: <N8nApiError text>"`.
  - `runtime_credential_deleted`: `"Deleted n8n credential '<name>' (<credential id>) at <host>: token <revoked|expired|not delivered>"`.
  - `runtime_credential_delete_failed`: `"Could not delete n8n credential '<name>' (<credential id>) at <host>: <N8nApiError text>"`.
  - `runtime_credential_orphaned`: `"n8n credential '<name>' (<credential id>) is at <old host>, which is no longer this server's n8n; its token was revoked. Delete the credential there by hand."`.

  `audit_log` renders these decisions as plain entries. None counts as an approval.

Two additions to `scoped_tokens.py`:

- `create(..., token_id: str | None = None)`. A given id must match `^st-[0-9a-f]{12}$` and be unused by that principal, else `ScopedTokenError("That token id is already in use.")`. `new_token_id()` returns `"st-" + secrets.token_hex(6)`.
- `ScopedTokenStore.token_state(principal: Principal, token_id: str, now: float | None = None) -> Literal["live", "expired", "absent", "unknown"]`. It refreshes the principal's file. `unknown` means that file exists but could not be read; `_reload` records that in a new `self._unreadable: set[str]`.

### 3.6 The meta tools

**`privacyfence_propose_scoped_token`** gains an optional property:

```json
"deliver_to": {"type": "object", "description": "Optional. Ask PrivacyFence to put the token into an agent runtime instead of giving it to the person: {\"runtime\": \"n8n\", \"workflow_id\": \"<the n8n workflow id, optional>\"}. Only on organization servers where the administrator set up n8n delivery. The workflow id is recorded for the audit log; n8n does not tie the credential to it."}
```

- `propose_scoped_token` handles it this way:
  - In local mode, or when `delivery_available(...)` is false: `ValueError(DELIVERY_UNAVAILABLE)`.
  - Otherwise it parses the value with `parse_deliver_to`. The proposal stores `delivery=DeliveryTarget(...)` and `mcp_url=mcp_url_for(config, base_url)`.
  - The `scoped_token_proposed` audit summary gains `", delivery to n8n"`.
  - The result gains `"delivery": {"runtime": "n8n", "workflow_id": "<id or empty>", "status_tool": "privacyfence_runtime_credential_status"}`, and its `message` becomes `DELIVERY_MESSAGE`.

  ```python
  DELIVERY_UNAVAILABLE = ("Delivery to n8n is not set up on this PrivacyFence server. "
                          "Your administrator sets it up under Admin → Agent runtimes.")
  DELIVERY_MESSAGE = ("Give the person this link. They review the token in PrivacyFence Settings, can narrow it, "
                      "and confirm it themselves. When they do, PrivacyFence creates an n8n credential holding the token; "
                      "call privacyfence_runtime_credential_status with this proposal_id to get the credential's id for "
                      "the MCP Client node. Anyone who can edit workflows in that n8n can use the credential, so keep its "
                      "tools and rules narrow. Calls that would need an approval are refused at once. Tools in "
                      "uncovered_tools have no rule, so the token's calls to them will be denied.")
  ```

**`privacyfence_runtime_credential_status`** is new: `RUNTIME_CREDENTIAL_STATUS_TOOL` in `web/mcp_tools.py`, appended to `META_TOOLS`, with annotations read-only, idempotent and not destructive.

- Input: `{"proposal_id": {"type": "string", "description": "The proposal_id privacyfence_propose_scoped_token returned for a proposal with deliver_to."}}`, required.
- Description, starting with this summary sentence: "Report whether a proposed token was delivered to n8n, and the n8n credential to use." The rest of the description follows the `assert_tool_definitions_complete` rules: a Returns sentence, that it is not offered to scoped tokens, and that the related tool is `privacyfence_propose_scoped_token`.
- Answers, all looked up only by `(current_principal().id, proposal_id)`:
  - The record is `delivered` and `token_state` is `live`:

    ```json
    {"state": "delivered", "runtime": "n8n", "credential_id": "...", "credential_name": "...",
     "credential_type": "httpBearerAuth", "node_authentication": "bearerAuth", "mcp_url": "...",
     "token_expires_at": "<ISO 8601>"}
    ```

  - The record is `creating`, or the proposal is still in the registry: `{"state": "pending", "expires_at": "<the proposal's, when known>"}`.
  - Anything else, including another person's proposal id, a declined or expired proposal, a revoked or expired token, an `abandoned` record and an unknown id: `{"state": "not_delivered"}`, with the same shape every time.
- It never returns a token id, a digest, the secret, or the n8n host beyond `mcp_url`.
- Listing: shown only when `scope is None` and `dispatcher.runtime_delivery_enabled()`, which is true in organization mode with proposals enabled and `delivery_available`. It is not added to `SCOPED_META_TOOLS`. At call time, the same three conditions are checked again: a scoped token gets `GateDeniedError("Request denied: a scoped token cannot read token deliveries.")`, local mode or delivery off gets `ValueError(DELIVERY_UNAVAILABLE)`.
- The agent is resolved like the propose tool's (`_resolve_agent`), and the call is audited as `runtime_credential_status_read` with no counts.

What the status tool reveals, stated in its ADR: whether the person accepted, a later revoke or expiry (`delivered` turns into `not_delivered`), the chosen lifetime, and the label (through `credential_name`). All of this is visible in n8n anyway.

### 3.7 The sweeper (`src/privacyfence/runtime_credential_sweeper.py`)

```python
SWEEP_INTERVAL_SECONDS = 300
DELETE_FAILURE_AUDIT_SECONDS = 86400

def sweep_once(*, records: RuntimeCredentialStore, token_store: ScopedTokenStore,
               config: N8nRuntimeConfig, api: N8nApi | None, now: float) -> None: ...
def start(*, config: N8nRuntimeConfig, stop_event: threading.Event) -> None: ...
def wake() -> None: ...
def stop() -> None: ...   # for tests and shutdown
```

`sweep_once` applies these rules in order, per record:

1. **The endpoint moved** (`record.endpoint != endpoint(config.base_url)`). Revoke the token if `token_state` is `live` or `expired`, audit `runtime_credential_orphaned` and remove the record. Never call DELETE on another host.
2. **No API** (`api is None`). Skip every record and log one warning per sweep.
3. **`delivered`.** `token_state`: `unknown` means skip, `live` means keep. `absent` or `expired` means `api.delete_credential`; then, whether it returned `True` or `False`, remove the record and audit `runtime_credential_deleted` with the reason `revoked` (absent) or `expired`.
4. **`creating`.** Skip it while `records.is_in_flight(record.credential_id)`. Otherwise it is from a crash or an earlier process: treat it as `abandoned`.
5. **`abandoned`.** If `token_state` is `live` or `expired`, revoke the token (`token_store.revoke(Principal(id=record.principal_id), record.token_id)`); if the revoke raises, keep the record. Then delete the credential, remove the record and audit `runtime_credential_deleted` with the reason `not delivered`.

- An `N8nApiError` keeps the record. `runtime_credential_delete_failed` is audited at most once per `DELETE_FAILURE_AUDIT_SECONDS` per credential id; the times are kept in memory.
- The thread wakes every `SWEEP_INTERVAL_SECONDS`, on `wake()`, and stops on `stop_event`.
- It is started whenever `config.base_url` and `api_key()` are set, whether or not `enabled` is true, so turning delivery off still cleans up.
- Reconciling against the token store covers every revoke path without a hook: the Settings revoke and revoke-all routes, `--revoke-scoped-tokens` (`daemon_main.py:2634`), `OAuthProvider.revoke_all_for_principal` (`web/oauth_provider.py:745`) and expiry. The organization revoke routes call `wake()` so the credential goes at once. The CLI does not touch the records file; the running daemon's sweeper deletes the credentials within `SWEEP_INTERVAL_SECONDS`, and the CLI prints a line saying so.
- `_INSTANCE` is module-level and reset in `tests/conftest.py`.

### 3.8 Settings and review page

- **Organization create-from-proposal** (`web/routes_settings.py:2036`). When `proposal.delivery` is set, it calls `runtime_credentials.deliver(...)` instead of `_create_and_deliver`, and answers:
  - on success, `{"created": {"id", "label", "expires_at"}, "delivered": {"runtime": "n8n", "credential_name": "<name>"}}` with `Cache-Control: no-store` and never `secret`;
  - otherwise `{"error": outcome.error}` with `outcome.status`.
  - It restores the proposal on 400 and 502, but not on 500. It records `scoped_token_proposal_accepted` only on success.
- **The local create-from-proposal route** (`:1343`) refuses a proposal whose `delivery` is set: 400 `{"error": "This proposal delivers the token to n8n, which needs an organization server."}`.
- **The organization revoke and revoke-all routes** call `runtime_credential_sweeper.wake()` after a successful revoke.
- **`scoped_token_proposals.review_data`** gains a keyword `runtime_host: str = ""` and a key `"delivery"`. It is `None` without delivery, otherwise `{"runtime": "n8n", "host": runtime_host, "project_id": "<id or ''>", "workflow_id": "<id or ''>"}`.
- **The review panel** (`scopedProposalPanelHtml`, `settings_window_html.py:1644`), when `p.delivery` is set, shows this text (with `<host>`, `<where>` and `<workflow>` filled in and escaped):

  "PrivacyFence will put this token into n8n at <host> as a credential, in <where>. Anyone who can edit workflows there, the AI client included, can use the token and read it until it expires or you revoke it. <workflow> Data the workflow reads is kept in n8n's execution history."

  - `<where>` is `"project <id>"`, or `"the personal space of the n8n API key's owner"` when no project is set.
  - `<workflow>` is `"The AI client says it is for workflow <id>; n8n does not tie the credential to it."` when an id was given, else empty.
- **The typed-name dialog** for a delivery proposal: its button reads "Create and deliver to n8n" and its first line reads "You are delegating access to n8n."
- **The client JS.** When `res.state.delivered` is present, it shows `"Created '<label>' and delivered it to n8n as '<credential name>'."` with `window.alert` before going to the section (the inline handler at `web/routes_settings.py:637-650`).
- **Token rows.** `scoped_token_page_data` (`web/server.py:355`) gains `runtime_credential` per token: `{"runtime": "n8n", "credential_name": "...", "workflow_id": "..."}` from `runtime_credentials.get_store().for_token(...)` when the record is `delivered`, else `null`. `scopedTokenRow` (`settings_window_html.py:1345`) shows `"In n8n as '<name>'"`, plus `" · for workflow <id> (as reported by the AI client)"` when there is one.

### 3.9 Admin page

- `admin_config`:
  - `_apply_agent_runtimes(config, params)` builds `{**config["agent_runtimes"], "n8n": {**current, "enabled": ..., "project_id": ..., "mcp_url": ...}}`, validates it with `_validated(N8nRuntimeConfig.from_org_config, "agent_runtimes", candidate)` and replaces the section with the summary `"Changed agent runtimes"`.
  - `"set_agent_runtimes"` is added to `_ACTION_MODES` (`_ORG_ONLY`) and `apply_change`.
- `routes_admin`:
  - `ADMIN_ACTIONS["set_agent_runtimes"] = AdminAction(_ORG_ONLY, True, "restart")` and `ADMIN_PARAMS["set_agent_runtimes"] = (("enabled", bool, True), ("project_id", str, True), ("mcp_url", str, True))`.
  - `RESTART_LABELS["agent_runtimes"] = "Agent runtimes"`, and `"agent_runtimes"` is added to `_ORG_STATE_KEYS`.
  - The organization state gains `"agent_runtimes_status": {"n8n": {"url": "<normalized PRIVACYFENCE_N8N_URL or ''>", "url_variable": "PRIVACYFENCE_N8N_URL", "key_variable": "PRIVACYFENCE_N8N_API_KEY", "key_set": <bool>}}`. A malformed URL shows as `""` with `"url_error": "<the ConfigurationError text>"`.
- `settings_window_html`:
  - `ADMIN_NAV_ITEMS` gets `['runtimes', 'Agent runtimes']` after `tokens`, and `ADMIN_FORMS` gets `runtimes: 'set_agent_runtimes'`.
  - A `renderRuntimes(state)` page holds:
    - a check row "Let AI clients ask for a token to be delivered into n8n." (field `enabled`, label "Turn on n8n delivery");
    - a text row "n8n project id" (field `project_id`), with the hint "Leave empty to use the API key owner's personal space.";
    - a text row "PrivacyFence MCP address for n8n" (field `mcp_url`), with the hint "Leave empty to use this server's address.";
    - two read-only rows: "n8n address" (the URL or "Not set: set PRIVACYFENCE_N8N_URL") and "n8n API key" ("Set" or "Not set: set PRIVACYFENCE_N8N_API_KEY").
  - `case 'runtimes'` is added to the admin router, and `"agent_runtimes": {"n8n": {"enabled": False, "project_id": "", "mcp_url": ""}}` to the defaults dict at `:3357`.

### 3.10 Rejected alternatives

These become ADR text.

- **A Wait node that blocks on `privacyfence_await_approval`.** Scoped tokens are deny-only already (ADR 0164), and runs that wait for days are an operational mess.
- **A new principal object for the runtime.** It would duplicate the token's expiry, revocation, audit identity and rules (ADR 0220's reasoning).
- **Hooking every revoke path.** The CLI and `revoke_all_for_principal` use separate code paths and a fresh store; reconciling against the store covers them all, including expiry.
- **The delivery inside the token entry.** Revoke deletes the entry, and its credential id with it, so the n8n side could never be cleaned up.
- **Editing the n8n address on the Admin page and clearing the key's variable on a move.** The key's variable has a default, so clearing it would resolve to the same key; the address comes from the environment instead, as for Peek and Grist.
- **Claude finding the credential by name with n8n's `list_credentials`.** It couples Claude to the n8n MCP and to a naming rule; the owner chose a status tool.
- **The New token form as a second entry point.** Only an AI proposal starts a delivery (owner's decision).
- **Forcing a passkey for a delivery confirmation.** It is the same gate as showing a new token's secret once in organization mode (ADR 0165); the risk added by delivery is n8n's reach, which the review text states.
- **`trust_env=False` on the n8n session.** It would break organization proxies; the Grist and Peek clients keep `requests`' defaults too.
- **Restricting the credential to the PrivacyFence host with n8n's credential domain allow-list.** It is not researched yet; it stays an open question (§6).

## 4. ADRs

The next free number in `docs/adr` is 0278.

- **0278** — A scoped token can be delivered into an agent runtime, n8n first, as a credential PrivacyFence writes itself. The token is the runtime's principal, and the workflow id is advisory. Delivery is organization-mode only and starts only from an AI proposal. Amends ADR 0165 (the secret is stored in n8n) and ADR 0250 (the client can use and read a delivered token through the runtime); relates to ADR 0164 and ADR 0220.
- **0279** — The n8n address and API key come only from the server environment. The address is https on any host except link-local, metadata and unspecified ones; the Admin page edits only `enabled`, `project_id` and `mcp_url`. It records the departure from ADR 0241's "a scoped key, not an admin key" for non-Enterprise n8n. Relates to ADR 0257 and ADR 0241.
- **0280** — `privacyfence_runtime_credential_status` reports a delivered proposal's credential to the client that proposed it. Amends ADR 0250's rejection of a status tool, for delivered proposals only.
- **0281** — A sweeper deletes the runtime credential of a revoked, expired or undelivered token by reconciling records against the token store, and a two-phase write keeps every live delivered token recorded. Relates to ADR 0164 and ADR 0165.

## 5. Manual steps

### Before implementation

None. Every phase builds and tests against a faked n8n (`requests.Session.request` monkeypatched, as `tests/unit/test_grist_provisioner_api.py` does).

### After implementation

#### ma1-n8n-end-to-end — Deliver, use and revoke a token against a real n8n (about 45 minutes)

1. Start a PrivacyFence organization server from the feature branch: either a single-user server on your machine as [the Docker guide](https://github.com/privacyfence/privacyfence/blob/feature/agent-runtime-credentials/docs/org-mode-docker.md) describes, or a source run as [the dev setup guide](https://github.com/privacyfence/privacyfence/blob/feature/agent-runtime-credentials/docs/dev-vs-live-setup.md) describes. It must be reachable from your n8n.
2. In n8n, open **Settings → n8n API** (`<your n8n>/settings/api`), choose **Create an API key**, and copy it. Create it as the same n8n user the n8n MCP server connects as, so that user's workflows can use the credential. On Enterprise n8n, give it only the scopes `credential:create` and `credential:delete`.
3. Put the key and the address into the PrivacyFence server's environment file (never into chat): `PRIVACYFENCE_N8N_API_KEY=<the key>` and `PRIVACYFENCE_N8N_URL=https://<your n8n host>`. If n8n uses an internal CA, also set `REQUESTS_CA_BUNDLE=<path to the CA file>`. Restart the server.
4. Sign in as an administrator, open **Admin → Scoped tokens**, and turn on both scoped tokens and AI token proposals. Then open **Admin → Agent runtimes**: you should see "n8n address" filled in and "n8n API key: Set". Tick **Turn on n8n delivery** and save; restart when asked.
5. In Claude Code, connected to this server as yourself, ask Claude to build an n8n workflow that calls one auto-approved PrivacyFence tool through an MCP Client node, using `privacyfence_propose_scoped_token` with `deliver_to: {"runtime": "n8n"}`.
6. Open the review link Claude gives you. The panel names your n8n host and warns that anyone who can edit workflows there can read the token. Continue, type the token's name, and confirm. You should see "Created '<label>' and delivered it to n8n as 'PrivacyFence <label>'."
7. Ask Claude to call `privacyfence_runtime_credential_status`. It should report `delivered` with a `credential_id`. In n8n, **Overview → Credentials** shows "PrivacyFence <label>" with that id.
8. Let Claude finish the workflow with that credential and run it once in n8n. It should succeed. The PrivacyFence audit log should show the call by `scoped-token:<id>` with the token's label, plus one `runtime_credential_delivered` entry.
9. Revoke the token in **Settings → Scoped tokens**. Within a minute the credential is gone from n8n's Credentials list, the audit log has `runtime_credential_deleted`, and a new run of the workflow fails.

Report back one line for the pull request: `ma1: pass` or `ma1: fail at step <n>: <what you saw>`. Also say whether n8n kept the credential id PrivacyFence chose (step 7), because that n8n behaviour is not researched.

## 6. Risks and open questions

- **n8n may ignore a caller-chosen credential id** on some versions, which is not researched. The client then refuses the answer (`did not create the credential as asked`), deletes the returned id and revokes the token, so delivery fails closed. If a worker's fake cannot follow the research facts in §3.3, it stops with `status=blocked`. ma1 step 7 checks the real behaviour.
- **The API key's user may not be the n8n MCP's user.** The credential would then not be usable in Claude's workflows. This is not researched; the docs and ma1 step 2 tell the administrator to use the same user or a shared project.
- **Credential domain allow-list.** n8n may support restricting a credential to certain hosts; that is not researched. It would narrow the read-through-n8n exposure in §3.1. It is a follow-up, not part of this plan.
- **Ambiguous n8n failures** (a timeout after the request was sent) leave a credential holding a revoked token until the sweeper deletes it. That is hygiene, not exposure: the token is dead in PrivacyFence first.
- **A crash between `store.create` and the POST** leaves a `creating` record from an earlier boot. The sweeper revokes its token and deletes the credential (404 is fine). A test covers it (p3).
- **A crash between writing the `creating` record and `store.create`** leaves a record whose token never existed. The sweeper finds the token `absent` and deletes nothing that exists. That is harmless.
- **An unreadable token file** makes `token_state` return `unknown`, and the sweeper skips it. A worker who finds `_reload`'s error path shaped differently from `scoped_tokens.py:556` stops with `status=blocked`.
- **The plan document** stays at `docs/1090/plan.md` as the playbook requires, although `CONTRIBUTING.md` says plans are deleted when work lands. The final review should confirm that this is acceptable, or add a deletion.
- **`tests/unit/test_docs_references_exist.py`** checks every `docs/...md` path in the repository, so this plan names ADRs by number only. The plan's final check names the deviations file, which implement creates when it starts.
- **Browser tests** are skipped in cloud sessions (no matching Chromium), so the review page and Admin form are proven by `test_settings_window_html.py` string checks and by ma1.

## Implementation manifest

```yaml
plan_slug: agent-runtime-credentials
feature_branch: feature/agent-runtime-credentials
tracking_issue: 1090
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-n8n-end-to-end
    title: Deliver, use and revoke a scoped token against a real n8n
    why: Only a real n8n proves that the credential id is kept, the credential works in an MCP Client node, and revocation deletes it; the unit tests fake n8n.
verify_after_merge:
  - python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q
final_checks:
  - "`tld plan deviations check --file docs/1090/deviations.md --plan docs/1090/plan.md --all` exits 0"
  - "ADRs 0278, 0279, 0280 and 0281 exist in docs/adr, are Accepted, and are listed in docs/adr/README.md"
  - "CHANGELOG.md has the new entries under ## [Unreleased] and no new version heading"
phases:
  - id: p1-config-and-api
    title: Runtime config, deliver_to parsing and the n8n API client
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/agent_runtimes.py
      - src/privacyfence/n8n_api.py
      - tests/unit/test_agent_runtimes.py
      - tests/unit/test_n8n_api.py
    brief: |
      Read first: docs/1090/plan.md sections 3.2, 3.3 and 3.4; docs/coding-and-testing-guidelines.md sections 1.4, 1.8 and 2.11.
      1. Create src/privacyfence/n8n_api.py exactly as plan section 3.3 specifies: N8nApiError(message, *, status=None),
         N8nApi(base_url, key) with create_bearer_credential, delete_credential and check, REQUEST_TIMEOUT_SECONDS = 30,
         allow_redirects=False on every call, the header X-N8N-API-KEY, the Idempotency-Key header on create, the four
         error texts verbatim, the 1,000,000-byte response cap, __repr__ showing the host only. Copy the session and
         error-mapping shape of src/privacyfence/grist_provisioner_api.py (GristProvisionerClient._send); never put a
         response body, header or request into an exception or a log line. Module docstring says what it is and that
         nothing logs the key or the token.
      2. Create src/privacyfence/agent_runtimes.py exactly as plan section 3.4 specifies (N8N_URL_ENV, N8N_API_KEY_ENV,
         RUNTIMES, N8nRuntimeConfig.from_org_config, DeliveryTarget, normalize_n8n_url, endpoint, api_key,
         delivery_available, parse_deliver_to with its four texts, credential_name, mcp_url_for, api_for). URL rules are in
         plan section 3.2's table; mcp_url rules in the bullet under it. Use org_mode.ConfigurationError for config errors
         and org_mode.provisioning_key for the key. No module-level mutable state. If the tld secret-scan hook blocks the
         N8N_API_KEY_ENV line, end it with the comment "# the variable's name, never a key; secret-scan:allow".
      3. tests/unit/test_n8n_api.py: fake requests.Session.request with a FakeResponse/Recorder pair copied from
         tests/unit/test_grist_provisioner_api.py:43-70. Classes: TestCreate (body, headers incl. Idempotency-Key, projectId
         only when set, 200 and 201 accepted, wrong id deletes the returned id then raises, wrong type raises),
         TestDelete (200/204 True, 404 False, 500 raises, a non-hex id raises ValueError before any request),
         TestErrors (each error text verbatim; RequestException; 302 raises and the Location is never requested;
         oversize body), TestNoSecretLeaks (caplog and str(error) never contain the key or the token on every failure
         path; repr shows the host only).
      4. tests/unit/test_agent_runtimes.py: TestFromOrgConfig (defaults; non-bool enabled, bad project_id, bad mcp_url
         and malformed env URL raise ConfigurationError; environ injected), TestNormalizeUrl (parametrized accepted and
         refused URLs from the table, including 169.254.169.254, [fe80::1], 0.0.0.0, metadata.google.internal, userinfo,
         query, fragment, http://), TestParseDeliverTo (each text), TestDeliveryAvailable, TestMcpUrlFor, TestApiFor
         (monkeypatch the env and the _FILE variant).
      5. ruff check ., and both test files at 100% branch coverage of the two modules.
      Stop with status=blocked if org_mode.provisioning_key or org_mode.ConfigurationError do not exist with the
      shapes plan section 2 describes.
    acceptance:
      - python3 -m pytest tests/unit/test_n8n_api.py tests/unit/test_agent_runtimes.py -q --cov=privacyfence.n8n_api --cov=privacyfence.agent_runtimes --cov-branch --cov-report=term-missing reports 100% for both modules
      - grep -n "allow_redirects=False" src/privacyfence/n8n_api.py matches
      - ruff check src/privacyfence/n8n_api.py src/privacyfence/agent_runtimes.py passes
      - python3 -m mypy src/privacyfence passes
      - bandit -c pyproject.toml -r src passes

  - id: p2-records-store
    title: Delivery records, the two-phase deliver flow and token-store additions
    depends_on: [p1-config-and-api]
    complexity: M
    touches:
      - src/privacyfence/runtime_credentials.py
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/scoped_token_proposals.py
      - tests/unit/test_runtime_credentials.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/conftest.py
    brief: |
      Read first: docs/1090/plan.md sections 3.5 and 3.6 (the status_for answers); docs/adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md; docs/coding-and-testing-guidelines.md section 2.3.
      1. scoped_tokens.py: add new_token_id(), the token_id keyword on ScopedTokenStore.create (validation and the exact
         error text in plan section 3.5), self._unreadable recorded by _reload, and token_state(principal, token_id,
         now=None) returning live|expired|absent|unknown. Extend tests/unit/test_scoped_tokens.py with TestChosenTokenId
         and TestTokenState (including a corrupt file giving "unknown").
      2. scoped_token_proposals.py: add to the frozen TokenProposal the fields delivery: DeliveryTarget | None = None and
         mcp_url: str = "" (after expires_at), a delivery and mcp_url keyword on new_proposal, and the runtime_host keyword
         and "delivery" key on review_data exactly as plan section 3.8 states. Extend
         tests/unit/test_scoped_token_proposals.py (TestReviewData: delivery None and set).
      3. Create src/privacyfence/runtime_credentials.py as plan section 3.5 specifies: the constants, BOOT_ID,
         RuntimeCredential, RuntimeCredentialStoreError, RuntimeCredentialStore (file rules, one lock, in_flight and
         is_in_flight, compare-and-set), get_store/set_store over paths.org_dir() / "agent_runtime_credentials.json",
         DeliveryOutcome, deliver() with steps 1-6 and the two texts verbatim, status_for() with the three answers of
         plan section 3.6, and the five audit decisions with their summaries (write them like
         scoped_token_proposals.record: principal_scope + audit_log.AuditEntry, failures logged and swallowed). Strip
         control characters from workflow_id and label in summaries. Step 6's "wake the sweeper" is a callable passed
         to deliver as on_abandoned: Callable[[], None] | None = None (the sweeper module comes in p3).
      4. tests/conftest.py _reset(): add runtime_credentials.set_store(None) next to the scoped-token lines (about 158-160).
      5. tests/unit/test_runtime_credentials.py, with a fake N8nApi object (no HTTP): TestStoreFile (round trip,
         0600 via secure_files, malformed file never rewritten and writes raise, set_state compare-and-set),
         TestDeliverHappyPath (record delivered, token exists with the chosen id, audit runtime_credential_delivered,
         no secret in the record file, caplog or audit), TestDeliverFailures (n8n error: token revoked, record abandoned,
         on_abandoned called, 502 text; revoke raises: record stays creating, 500 text; ScopedTokenError: record removed,
         400; record vanished before set_state: token revoked, credential delete attempted), TestCrashPoints (stop after
         each of steps 2-4 by raising from a fake; afterwards no live token lacks a record), TestStatusFor (delivered
         needs a live token; creating and a registry hit are pending; another principal's record, abandoned, expired
         token, unknown id are all exactly {"state": "not_delivered"}; no token_id in any answer).
      Stop with status=blocked if ScopedTokenStore.create's signature differs from scoped_tokens.py:595 as described.
    acceptance:
      - python3 -m pytest tests/unit/test_runtime_credentials.py tests/unit/test_scoped_tokens.py tests/unit/test_scoped_token_proposals.py -q passes
      - python3 -m pytest tests/unit/test_runtime_credentials.py -q --cov=privacyfence.runtime_credentials --cov-branch --cov-report=term-missing reports 100%
      - grep -n "runtime_credentials.set_store(None)" tests/conftest.py matches
      - python3 -m mypy src/privacyfence passes

  - id: p3-sweeper
    title: The cleanup sweeper
    depends_on: [p2-records-store]
    complexity: M
    touches:
      - src/privacyfence/runtime_credential_sweeper.py
      - tests/unit/test_runtime_credential_sweeper.py
      - tests/conftest.py
    brief: |
      Read first: docs/1090/plan.md section 3.7; src/privacyfence/peek_provisioning.py (thread start and stop pattern).
      1. Create src/privacyfence/runtime_credential_sweeper.py as plan section 3.7 specifies: SWEEP_INTERVAL_SECONDS,
         DELETE_FAILURE_AUDIT_SECONDS, sweep_once with rules 1-5 in that order, start, wake, stop and a module-level
         _INSTANCE. The thread is a daemon thread that waits on an Event with timeout SWEEP_INTERVAL_SECONDS, sweeps,
         and exits when stop_event is set. Audit through the helpers runtime_credentials already has. wake() is a no-op
         when no sweeper runs.
      2. tests/conftest.py _reset(): call runtime_credential_sweeper.stop() and set _INSTANCE to None.
      3. tests/unit/test_runtime_credential_sweeper.py with a fake api and a real ScopedTokenStore over tmp_path:
         one test per rule (endpoint moved: token revoked, orphaned audit, no DELETE; api None: nothing removed;
         delivered + live kept; + absent deleted with reason revoked; + expired deleted with reason expired; + unknown
         skipped; creating in flight skipped; creating from an earlier boot revoked and deleted; abandoned with a
         revoke that raises kept), TestDeleteFailures (record kept, delete_failed audited once within 24 h, again
         after), TestThread (wake() runs a sweep promptly; stop_event ends the thread; bounded waits, no sleeps over 1 s).
    acceptance:
      - python3 -m pytest tests/unit/test_runtime_credential_sweeper.py -q --cov=privacyfence.runtime_credential_sweeper --cov-branch --cov-report=term-missing reports 100%
      - grep -n "runtime_credential_sweeper" tests/conftest.py matches

  - id: p4-admin
    title: The Agent runtimes section of the Admin page
    depends_on: [p1-config-and-api]
    complexity: M
    touches:
      - src/privacyfence/admin_config.py
      - src/privacyfence/web/routes_admin.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_admin_config.py
      - tests/unit/web/test_routes_admin.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read first: docs/1090/plan.md sections 3.2 and 3.9; docs/adr/0257-operational-settings-live-in-a-daemon-written-admin-config.md.
      Pattern to copy: how set_scoped_tokens is wired end to end (admin_config._apply_scoped_tokens at about 853,
      routes_admin ADMIN_ACTIONS/ADMIN_PARAMS, settings_window_html renderTokens at about 2523).
      1. admin_config.py: add the agent_runtimes section to _default_config() and "agent_runtimes" to
         OPERATIONAL_SECTIONS; add _apply_agent_runtimes and "set_agent_runtimes" to _ACTION_MODES and apply_change as plan
         section 3.9 states.
      2. web/routes_admin.py: the ADMIN_ACTIONS, ADMIN_PARAMS, RESTART_LABELS and _ORG_STATE_KEYS entries, and
         agent_runtimes_status in the organization state (plan section 3.9; read the URL with
         agent_runtimes.normalize_n8n_url on os.environ.get(N8N_URL_ENV, "") and the key with agent_runtimes.api_key();
         never put the key value in the state).
      3. settings_window_html.py: the nav item, ADMIN_FORMS entry, renderRuntimes with the rows and strings verbatim from
         plan section 3.9, the router case and the defaults entry.
      4. Tests: tests/unit/test_admin_config.py TestAgentRuntimes (default present; a change saves the three fields; a
         bad project_id or mcp_url is refused with AdminConfigError); tests/unit/web/test_routes_admin.py (the action is
         org-only, sensitive, needs a passkey, a base_url or key-variable field in the request is refused as unknown,
         restart is flagged with "Agent runtimes", the state carries agent_runtimes_status and never the key's value);
         tests/unit/test_settings_window_html.py (the nav item and each string appear; the address and key rows are not
         inputs). Add one test that oauth_apps.effective_org_config carries the agent_runtimes section through.
      Stop with status=blocked if OPERATIONAL_SECTIONS changes what a legacy bundle import copies in a way the existing
      tests/unit/test_admin_config.py import tests reject.
    acceptance:
      - python3 -m pytest tests/unit/test_admin_config.py tests/unit/web/test_routes_admin.py tests/unit/test_settings_window_html.py -q passes
      - grep -n "set_agent_runtimes" src/privacyfence/web/routes_admin.py src/privacyfence/admin_config.py src/privacyfence/settings_window_html.py matches in all three
      - python3 -m pytest tests/unit/test_design_system.py -q passes

  - id: p5-mcp-tools
    title: deliver_to on the propose tool and the status meta tool
    depends_on: [p2-records-store]
    complexity: M
    touches:
      - src/privacyfence/web/mcp_tools.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/web/routes_mcp.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - tests/unit/web/test_mcp_tools.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_routes_mcp.py
    brief: |
      Read first: docs/1090/plan.md section 3.6; docs/adr/0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md.
      Pattern commit: 6b7e23466 (how privacyfence_propose_scoped_token was added).
      1. web/mcp_tools.py: add the deliver_to property to PROPOSE_SCOPED_TOKEN_TOOL's schema (text verbatim from plan
         section 3.6) and the new RUNTIME_CREDENTIAL_STATUS_TOOL, appended to META_TOOLS.
      2. web/mcp_dispatch.py: McpDispatcher.__init__ gains agent_runtime: N8nRuntimeConfig | None = None; add
         runtime_delivery_enabled(); in propose_scoped_token handle deliver_to exactly as plan section 3.6 states
         (DELIVERY_UNAVAILABLE and DELIVERY_MESSAGE verbatim, local mode refused, mcp_url_for, audit suffix, result key);
         add runtime_credential_status(params) calling runtime_credentials.status_for with the call-time refusals of
         plan section 3.6 and the runtime_credential_status_read audit entry.
      3. web/routes_mcp.py: list the status tool only when scope is None and dispatcher.runtime_delivery_enabled();
         dispatch it in _dispatch_meta_tool; resolve its agent like the propose tool's. Do not add it to SCOPED_META_TOOLS.
      4. scripts/generate_tools_reference.py: replace the literal at about line 211 with "eight always, plus
         `privacyfence_propose_scoped_token` where token proposals are on (always on a local install, by the
         administrator's choice in organization mode) and `privacyfence_runtime_credential_status` where n8n delivery
         is also set up"; run python3 scripts/generate_tools_reference.py and commit docs/tools-reference.md.
      5. Tests: test_mcp_tools.py (the new tool is in META_TOOLS, read-only, requires proposal_id; deliver_to is in the
         propose schema); test_mcp_dispatch.py TestProposeWithDelivery (accepted in org mode with delivery available;
         refused in local mode, with delivery off, with an unknown key, another runtime, a bad workflow id; the proposal
         carries delivery and mcp_url; the message is DELIVERY_MESSAGE) and TestRuntimeCredentialStatus (each answer;
         a scoped token is refused; local mode refused; another principal's proposal is not_delivered);
         test_routes_mcp.py (listed only with delivery on and no scope; a scoped token never sees or can call it,
         including over the wire; tool definitions pass assert_tool_definitions_complete).
      6. Run python3 -m pytest tests/integration -v; test_mcp_daemon_contract.py must pass (definition of done row for
         routes_mcp.py and mcp_dispatch.py).
      Stop with status=blocked if handle_list_tools or _dispatch_meta_tool are not shaped as plan section 2 describes.
    acceptance:
      - python3 -m pytest tests/unit/web/test_mcp_tools.py tests/unit/web/test_mcp_dispatch.py tests/unit/web/test_routes_mcp.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py -q passes
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q passes
      - grep -n "privacyfence_runtime_credential_status" src/privacyfence/web/routes_mcp.py matches and the SCOPED_META_TOOLS block does not contain it

  - id: p6-settings-delivery
    title: Delivery on the organization create-from-proposal route and the Settings page
    depends_on: [p3-sweeper, p4-admin, p5-mcp-tools]
    complexity: M
    touches:
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/server.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_server.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read first: docs/1090/plan.md sections 3.1 and 3.8; docs/adr/0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md.
      1. web/routes_settings.py build_org_routes gains agent_runtime: N8nRuntimeConfig | None = None. In
         scoped_token_create_from_proposal (about 2036), when proposal.delivery is set, call runtime_credentials.deliver
         with agent_runtimes.api_for(agent_runtime) and on_abandoned=runtime_credential_sweeper.wake (an api of None is a
         502 with DELIVERY_FAILED and nothing minted) and answer as plan section 3.8 states; keep the step-up, typed-name
         and narrowing gates as they are. Never put "secret" in that answer. The org revoke and revoke-all routes call
         runtime_credential_sweeper.wake() after a successful revoke. The local route (about 1343) refuses a delivery
         proposal with the 400 text of plan section 3.8. The inline client JS (about 637-650) shows the delivered alert
         text verbatim before going to the section.
      2. web/server.py: parse agent_runtimes.N8nRuntimeConfig.from_org_config(org.org_config) next to
         scoped_token_policy (about 1456) and pass it to build_org_routes; scoped_token_page_data (about 355) adds
         runtime_credential per token; the proposal review page passes runtime_host (the config's URL host) to review_data.
      3. settings_window_html.py: the review panel text, the dialog line and button text, and the token row text,
         verbatim from plan section 3.8, all escaped with the existing esc helper.
      4. Tests: test_routes_org_settings.py TestDeliveryFromProposal (success answer has delivered and no secret; the n8n
         error gives 502 and restores the proposal; revoke failure gives 500 and does not restore; a request body that
         carries deliver_to or a delivery field does not change the target; plain proposals still return the secret
         once; revoke wakes the sweeper); test_routes_settings.py (local route refuses a delivery proposal);
         test_server.py (runtime_credential in page data for a delivered token, null otherwise);
         test_settings_window_html.py (each string present).
      Stop with status=blocked if _create_and_deliver or scoped_token_create_from_proposal differ from plan section 2.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_org_settings.py tests/unit/web/test_routes_settings.py tests/unit/web/test_server.py tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/unit/test_design_system.py tests/unit/test_systemic_gate_invariants.py -q passes

  - id: p7-daemon-wiring
    title: Start-up validation, the dispatcher config and the sweeper in the daemon
    depends_on: [p3-sweeper, p5-mcp-tools]
    complexity: S
    touches:
      - src/privacyfence/daemon_main.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read first: docs/1090/plan.md sections 3.2 and 3.7.
      1. daemon_main.py: in the organization start-up, next to the Peek and Grist validation (about 1379-1391), build
         agent_runtime = agent_runtimes.N8nRuntimeConfig.from_org_config(org_config) so a malformed section or URL
         refuses start-up; log one warning when enabled is true but the URL or key is missing. Pass agent_runtime to
         McpDispatcher (about 1493). Next to peek_provisioning.start (about 1459) call
         runtime_credential_sweeper.start(config=agent_runtime, stop_event=_shutdown_event) when agent_runtime.base_url
         and agent_runtimes.api_key() are set, and call agent_runtimes.api_for(agent_runtime).check() once in a try that
         logs a warning (never the key) on N8nApiError.
      2. run_revoke_scoped_tokens (about 2634): after revoking, print "A running PrivacyFence server deletes these
         tokens' n8n credentials within 5 minutes."
      3. tests/unit/test_daemon_main.py: a malformed agent_runtimes section refuses org start-up; the sweeper starts
         only with URL and key; the dispatcher receives the config; the CLI prints the line.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py -q passes
      - grep -n "runtime_credential_sweeper.start" src/privacyfence/daemon_main.py matches

  - id: p8-docs-adrs
    title: Docs, ADRs and changelog
    depends_on: [p6-settings-delivery, p7-daemon-wiring]
    complexity: M
    touches:
      - docs/org-mode-setup-guide.md
      - docs/security-and-compliance.md
      - docs/how-it-works.md
      - docs/connect-claude-code.md
      - docs/configuration-reference.md
      - docs/adr/*.md
      - CHANGELOG.md
    brief: |
      Read first: docs/1090/plan.md sections 3, 4 and 6; docs/adr/README.md (template and rules); CONTRIBUTING.md.
      1. Write the four ADRs of plan section 4 with the template in docs/adr/README.md, Status "Accepted — <merge date>.
         Implemented.", the decision and rejected alternatives taken from plan sections 3.1-3.10, the research facts with
         their URLs and the date 2026-11-13, and Verification lines naming the test classes of phases p1-p7. Add the
         forward Status line to ADR 0165 ("Amended by ADR 0278: a delivered token's secret is stored in n8n.") and to
         ADR 0250 ("Amended by ADR 0278 and ADR 0280: a delivered token can be read through the runtime, and its
         credential id is reported."), the only edit allowed to an accepted ADR. Add four index rows to docs/adr/README.md.
      2. docs/org-mode-setup-guide.md, section 11 "AI systems": a subsection "### Delivering tokens to n8n" covering the
         two environment variables, the minimum n8n version 2.40.0, creating the API key as the n8n MCP's user (Enterprise:
         only credential:create and credential:delete; other editions: full access), REQUESTS_CA_BUNDLE for an internal
         CA, a dedicated n8n project where the edition has projects, Admin → Agent runtimes, and that revoking or expiry
         deletes the credential within 5 minutes.
      3. docs/security-and-compliance.md: a section on delivered tokens stating plan section 3.1's exposure (readable by
         any n8n workflow author, the AI client included), that the key never leaves the environment, what the status tool
         reveals, that workflow data is kept in n8n, and the offboarding note (revoking deletes the n8n credentials).
      4. docs/how-it-works.md "PrivacyFence's own tools" and docs/connect-claude-code.md: the new tool and deliver_to.
      5. docs/configuration-reference.md: the agent_runtimes admin section and the two environment variables, if that
         doc lists the other admin sections and variables; otherwise leave it unchanged and say so in the report.
      6. CHANGELOG.md under ## [Unreleased]: one "Added" line for n8n delivery of scoped tokens and the status tool.
      7. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_docs_configuration_reference.py -q.
    acceptance:
      - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_docs_configuration_reference.py -q passes
      - grep -n "0278" docs/adr/README.md matches
      - grep -n "n8n" CHANGELOG.md matches under ## [Unreleased]
```
