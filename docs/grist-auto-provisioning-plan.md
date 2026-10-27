# Grist auto-provisioning in organization mode — plan

## Goal

Today, an organization that runs free, self-hosted grist-core offers Grist on `/connect` with a personal API key. Each person opens Grist, creates a key under **Account settings → Developer → API Key** and pastes it into PrivacyFence.

With this change, an organization can turn on **Grist auto-provisioning** in its bundle. When a person signs in to PrivacyFence with a verified email and already has a Grist account, PrivacyFence registers a Grist API key for that account as their Grist connection, with no clicks. Grist creates the account itself, at the person's first Google sign-in to Grist or when someone shares a document with their address.

grist-core has no API that creates a key for another user, and the free edition has no OAuth apps. So the key comes from a small **Grist provisioner** service that runs next to Grist, in Grist's network zone. It sets or reads the person's key directly in Grist's home database (SQLite). PrivacyFence calls it with a scoped **provisioning key**. The provisioner never connects to PrivacyFence.

This is the same shape as Peek auto-provisioning (`docs/peek-auto-provisioning-plan.md` on `plan/peek-auto-provisioning`). Each product keeps working without the other:

- PrivacyFence sign-in never waits on Grist or the provisioner; an outage only delays provisioning.
- Grist keeps its own users, its own Google sign-in and its own keys.

Grist allows exactly one API key per person, and that shapes the rules:

- **No key in Grist yet.** The provisioner creates one, called *managed*. It expires 90 days after it was last issued unless PrivacyFence renews it, and `--grist-release` deletes it in Grist.
- **The person already made a key in Grist.** The provisioner *adopts* it and hands it to PrivacyFence, as the user decided. PrivacyFence never deletes an adopted key, because the person's own scripts use it.
- **A key the person pasted on `/connect`** always wins; provisioning leaves it alone.

The provisioner lives in a **new repository**, as the user decided. Its build is the companion plan `docs/grist-provisioner-companion-plan.md`, written on this branch and to be moved into that repository (see "Manual steps"). This plan builds against the wire contract below and against fakes. No issue tracks it.

## Current state

- **Grist bundle section.**
  - `grist_auth.bundle_settings` (`src/privacyfence/grist_auth.py:122-145`) reads `grist.server_url` and the optional OAuth `client_id`/`client_secret`. In org mode it raises `GristClientError("Grist organization config not installed")` without a server.
  - `grist_auth.normalize_server_url` (`:75-98`) enforces https (http only for loopback) and rejects a user name, password, query, fragment or an `/api` path segment.
  - The builder writes the section at `scripts/build_org_bundle.py:688-707`. `:705` assigns `bundle["grist"] = grist_section`, which replaces the whole section. `_grist_url` (`:79-99`) mirrors `normalize_server_url`.
  - The Grist argument group starts at `:284`. The `--web-push` mode check is at `:840-843`; `--mode local` strips org-only sections at `:765-773`.
  - `tests/unit/test_docs_configuration_reference.py` requires every builder option in `docs/configuration-reference.md`. The Grist rows are at `:286-289`; the daemon option table is at `:357-368` (`--grist-oauth` at `:364`).
- **Org config classes.** `WebPushConfig` (`src/privacyfence/org_mode.py:491-514`) is the pattern for strict validation; `__all__` is at `:538-559`. `org_mode.py` imports neither `re` nor `grist_auth` today. `grist_auth` does not import `org_mode`, so there is no import cycle.
- **Grist credential file.** `credentials/grist_token.json` (`TOKEN_FILES`, `src/privacyfence/daemon_main.py:209`).
  - `grist_auth.save_token_file` (`:157-159`) is the one write site (`tests/unit/test_systemic_gate_invariants.py:190`).
  - `save_api_key` (`:162-166`) writes `{"auth": "api_key", "server_url", "api_key"}`.
  - `load_token_file` (`:169-189`) checks `_REQUIRED_KEYS` and returns the record with `server_url` normalised.
  - `resolve_credential` (`:212-232`) pairs an `api_key` record with the pinned server.
  - The connector is built at `daemon_main.py:1711-1728`.
- **`/connect`.**
  - `grist_connect` (`src/privacyfence/web/routes_connect.py:745-776`) checks a pasted key with `GristClient.check_connection` and saves it with `save_api_key` at `:766-768`. Then it calls `connector_registry.evict(principal.id)`.
  - `_grist_box_html` (`:1000-1028`) draws the API-key form.
  - The tests are `TestGristBox` (`tests/unit/web/test_routes_connect.py:953`) and `TestGristApiKey` (`:1145`).
- **Principals and sign-in.**
  - `org_identity.principal_from_claims` (`src/privacyfence/org_identity.py:279-299`) copies `claims["email"]` without reading `email_verified`.
  - Two callbacks hold both the principal and the claims:
    - `routes_org_identity.login_callback` (`src/privacyfence/web/routes_org_identity.py:200-238`; policy check at `:223`, `sessions.create` at `:227`);
    - `OrgOAuthProvider.handle_idp_callback` (`src/privacyfence/web/oauth_provider.py:441-488`; policy check at `:476`).
  - Principal ids match `^[A-Za-z0-9._@-]{1,200}$` (`paths.py:46`). `web/mcp_auth.py:208-220` lists the `users/<id>/` directories.
- **Background work and offboarding.**
  - `_run_personal_data_sweep` (`daemon_main.py:311-322`) is the guarded-loop pattern; `_shutdown_event` is at `:268`.
  - `_start_org_web_server` (`:1109-1265`) creates `connector_registry` at `:1210`.
  - `--revoke-scoped-tokens` is the offboarding precedent: `run_revoke_scoped_tokens` at `:2320-2346`, the argument at `:2405`, dispatch at `:2497`.
- **Coverage floors** (`scripts/check_coverage_floor.py`): `org_mode.py` 100.0, `daemon_main.py` 96.0, `oauth_provider.py` 99.0, `routes_org_identity.py` 99.0, `routes_connect.py` 91.0.
- **Test resets.** `tests/conftest.py` `_reset()` (`:90-145`) already clears `grist_auth._DISCOVERY_CACHE` (`:100`); its import list is at `:27-46`.
- **Changelog.** `## [Unreleased]` is empty (`CHANGELOG.md:44`).
- **ADRs.** The last is 0238. The Peek plan reserves 0239-0241 and has not landed. The Grist ADRs today are 0142, 0143 and 0196.
- **grist-core facts** (checked at gristlabs/grist-core `23043a18`, version 1.7.21):
  - `users.api_key` is a nullable, plaintext column, one key per user (`app/gen-server/entity/User.ts:25-28`). Grist generates keys as `crypto.randomBytes(20).toString("hex")` (`UsersManager.ts:37-39`).
  - Every API request looks the key up in the database with no cache (`Authorizer.ts:187-217`, `UsersManager.ts:168-170`), so a key written to the database works at once and a deleted one fails at once.
  - Disabled users (`users.disabled_at`) get 403. Install admin is `GRIST_ADMIN_EMAIL`, else `GRIST_DEFAULT_EMAIL` (`InstallAdmin.ts:102-116`).
  - Emails are normalised to lower case in `logins.email` (`app/common/emails.ts:40-43`).
  - The Docker image keeps the home database at `/persist/home.sqlite3` (`Dockerfile:192`).
  - Forward auth (`GRIST_FORWARD_AUTH_HEADER`) is a *login system* that replaces Grist's own OIDC sign-in (`ForwardAuthLogin.ts:20-66`).

## Design

### The provisioner's wire contract

The companion plan implements this contract; this plan builds the client against it and tests with fakes.

- Every request carries `Authorization: Bearer <provisioning key>`; the key starts with `gristprov_`.
- `{external_id}` is the PrivacyFence principal id and matches `^[A-Za-z0-9._@-]{1,200}$`.
- Every error answer is JSON `{"error": "…"}`, and an unknown route answers 404 with a plain-text body.
- `key_id` is the first 12 hex characters of the SHA-256 of the key, so PrivacyFence can tell whether the key it holds is still the person's current key without the provisioner sending the key.

| Call | Body | Success | Errors PrivacyFence handles |
|---|---|---|---|
| `GET /api/provisioning/self` | none | 200 `{"name", "accounts"}` | 401 |
| `PUT /api/provisioning/accounts/{external_id}` | `{"email"}` | 200 `{"external_id", "user_id", "disabled", "has_key", "managed", "key_id", "expires_at"}` (`key_id` is `""` when `has_key` is false; `expires_at` is unix seconds, 0 when not managed) | 400; 401; 403 `account email domain is not allowed`; 404 `no Grist account for this email`; 409 `account is a Grist admin; connect it by hand`; 409 `account is linked to another external id or email` |
| `POST /api/provisioning/accounts/{external_id}/key` | `{}` | 201 `{"key", "managed", "key_id", "expires_at"}` | 403 `account disabled`; 404 `not found` (no link); 409 admin |
| `DELETE /api/provisioning/accounts/{external_id}` | none | 200 `{"released": true, "revoked": <bool>}` | 404 `not found` (no link) |

What `POST …/key` returns depends on the account's key in Grist:

| The account in Grist has | The call returns |
|---|---|
| no key | A new key the provisioner created: `managed: true`, `expires_at = now + 90 days`. |
| the provisioner's own managed key | The same key, with `expires_at` pushed to `now + 90 days`. |
| a key the person made | That key, adopted: `managed: false`, `expires_at: 0`. |

The provisioner deletes a managed key in Grist when it expires or on `DELETE`, but only while it is still the account's current key. `revoked` says whether that happened.

### Organization config

```json
"grist": {
  "server_url": "https://grist.example.com",
  "auto_provision": {
    "enabled": true,
    "provisioner_url": "https://grist-provisioner.example.com",
    "key_env": "PRIVACYFENCE_GRIST_PROVISIONING_KEY",
    "trust_unverified_email": false
  }
}
```

Add to `src/privacyfence/org_mode.py`, after `WebPushConfig`. Add `import re` and `from .grist_auth import GristClientError, normalize_server_url`, and put the public names in `__all__`:

```python
DEFAULT_GRIST_PROVISIONING_KEY_ENV = "PRIVACYFENCE_GRIST_PROVISIONING_KEY"
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")

@dataclass(frozen=True)
class GristProvisioningConfig:
    enabled: bool = False
    key_env: str = DEFAULT_GRIST_PROVISIONING_KEY_ENV
    trust_unverified_email: bool = False
    provisioner_url: str = ""   # normalised; set only when enabled
    server_url: str = ""        # the normalised grist.server_url; set only when enabled

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "GristProvisioningConfig": ...
```

`from_org_config` reads `grist` (a dict, else `{}`), then `auto_provision` (a dict, else `{}`). With no `auto_provision` it returns `GristProvisioningConfig()`. Otherwise it checks these in order, raising `ConfigurationError` with exactly the given message:

1. `enabled` or `trust_unverified_email` present and not a bool: `org_config.json's "grist"."auto_provision"."<key>" must be true or false, got {value!r}`
2. `key_env` present and not fully matching `_ENV_NAME_RE`: `org_config.json's "grist"."auto_provision"."key_env" must be an environment variable name, got {value!r}`
3. Not enabled: return `GristProvisioningConfig(enabled=False)`.
4. `resolve_mode(org_config) != "org"`: `org_config.json's "grist"."auto_provision" needs "mode": "org"`
5. `grist.server_url` empty: `org_config.json's "grist"."auto_provision" needs "grist"."server_url"`
6. `grist.client_id` non-empty: `org_config.json's "grist"."auto_provision" works only with API keys; remove the Grist OAuth app or turn auto-provisioning off`
7. `provisioner_url` empty: `org_config.json's "grist"."auto_provision" needs "provisioner_url"`
8. `normalize_server_url(grist.server_url)` raises `GristClientError` as `exc`: `org_config.json's "grist"."server_url" is not usable: {exc}`
9. `normalize_server_url(provisioner_url)` raises as `exc`: `org_config.json's "grist"."auto_provision"."provisioner_url" is not usable: {exc}`

The key itself is never in the bundle, for the same reason as `http_bearer_token_env`. The daemon reads `os.environ[key_env]`.

**Verified email.** By default only a principal whose ID token has `email_verified` set to the JSON `true` or the string `"true"` is provisioned. Google always sends the claim. `trust_unverified_email: true` is for providers that never send it but only issue verified, organization-owned addresses. Without the check, anyone who can set their own email at the IdP could get another person's Grist key.

#### Builder (`scripts/build_org_bundle.py`, Grist argument group at `:284`)

New flags, with these help texts:

| Flag | Kind | Help |
|---|---|---|
| `--grist-auto-provision` / `--no-grist-auto-provision` | `argparse.BooleanOptionalAction`, `default=None` | `Give each person a Grist API key at sign-in through the Grist provisioner and connect it for them (organization mode, API keys only; needs --grist-server-url, --grist-provisioner-url and the provisioning key in the daemon's environment).` |
| `--grist-provisioner-url URL` | | `The Grist provisioner service PrivacyFence calls (https://; http:// only for localhost).` |
| `--grist-provisioning-key-env NAME` | | `The environment variable the daemon reads the Grist provisioning key from (default PRIVACYFENCE_GRIST_PROVISIONING_KEY).` |
| `--grist-trust-unverified-email` | `store_true` | `Provision people whose ID token does not say email_verified (only for identity providers that issue only verified, organization-owned addresses). Give it again on every --grist-auto-provision rebuild; leaving it out turns it off.` |

Rules:

- **Keep the section on a rebuild.** At `:697-705`, before `bundle["grist"] = grist_section`, carry over the old `auto_provision`: `previous = bundle.get("grist")`; if it is a dict and has `"auto_provision"`, set `grist_section["auto_provision"] = previous["auto_provision"]`. A `--merge --grist-server-url NEW` therefore keeps it.
- **Where the checks go.** Next to the `--web-push` check (`:840-843`), after `bundle["mode"]` is known. Let `existing = bundle.get("grist", {}).get("auto_provision")`, or `{}` when that is not a dict. When `args.grist_auto_provision is not None`, check these in order:
  1. `bundle.get("mode") != "org"` → `SystemExit("--grist-auto-provision/--no-grist-auto-provision require --mode org (or --merge against an existing org-mode bundle).")`
  2. `args.grist_auto_provision` is false → set `bundle.setdefault("grist", {})["auto_provision"] = {"enabled": False}` and stop here. Turning it off never needs a server or refuses an OAuth app.
  3. No `bundle.get("grist", {}).get("server_url")` → `SystemExit("--grist-auto-provision requires --grist-server-url (or --merge against a bundle that has one).")`
  4. `bundle["grist"].get("client_id")` → `SystemExit("--grist-auto-provision works only with API keys; it cannot be combined with --grist-client-id.")`
  5. `url = args.grist_provisioner_url or existing.get("provisioner_url")`. If empty → `SystemExit("--grist-auto-provision requires --grist-provisioner-url.")`
  6. Otherwise set `bundle["grist"]["auto_provision"] = {"enabled": True, "provisioner_url": _grist_url(url), "key_env": args.grist_provisioning_key_env or existing.get("key_env") or _DEFAULT_GRIST_PROVISIONING_KEY_ENV, "trust_unverified_email": args.grist_trust_unverified_email}`.
- **Companion flags alone.** When `args.grist_auto_provision is None` but `--grist-provisioner-url`, `--grist-provisioning-key-env` or `--grist-trust-unverified-email` is given → `SystemExit("--grist-provisioner-url, --grist-provisioning-key-env and --grist-trust-unverified-email go with --grist-auto-provision.")`
- **`--mode local`** (`:765-773`) also removes it: `if isinstance(bundle.get("grist"), dict): bundle["grist"].pop("auto_provision", None)`.
- **The default key name.** `_DEFAULT_GRIST_PROVISIONING_KEY_ENV = "PRIVACYFENCE_GRIST_PROVISIONING_KEY"` is a module constant, with a comment naming `org_mode.DEFAULT_GRIST_PROVISIONING_KEY_ENV`. The builder imports nothing from `src/`.

### The credential file (`grist_auth`)

`save_api_key(token_file, server_url, api_key, *, source="manual", managed=False, expires_at=0)` writes `{"auth": "api_key", "server_url", "api_key"}`. Only when `source == "provisioned"` does it add `"source": "provisioned"`, `"managed": <bool>` and `"expires_at": <int>`. A manual file stays byte-identical to today's.

`load_token_file` adds these checks for an `api_key` record that has a `source` key. Each failure raises `GristClientError(_UNREADABLE)`:

- `source` must be `"provisioned"`;
- `managed` must be a bool;
- `expires_at` must be an int and not a bool.

New helpers:

- `token_source(record: dict[str, Any]) -> str` returns `"provisioned"` when `record.get("source") == "provisioned"`, else `"manual"`.
- `key_id(api_key: str) -> str` returns `hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]`.

`resolve_credential` is unchanged.

### `src/privacyfence/grist_provisioner_api.py` (new)

The module name deliberately does not end in `_client.py`. This is not a connector and has no QA account, so the `dod_conditional` QA-fixture row does not apply.

```python
class GristProvisionerClientError(Exception):
    def __init__(self, message: str, *, status: int | None = None, detail: str = "") -> None

@dataclass(frozen=True)
class GristProvisionedAccount:
    external_id: str
    user_id: int
    disabled: bool
    has_key: bool
    managed: bool
    key_id: str
    expires_at: int

@dataclass(frozen=True)
class GristIssuedKey:
    key: str = field(repr=False)
    managed: bool
    key_id: str
    expires_at: int

@dataclass(frozen=True)
class GristRelease:
    released: bool
    revoked: bool

class GristProvisionerClient:
    def __init__(self, base_url: str, key: str) -> None
    def check(self) -> int                                          # GET  /api/provisioning/self -> accounts
    def upsert_account(self, external_id: str, email: str) -> GristProvisionedAccount
    def issue_key(self, external_id: str) -> GristIssuedKey         # POST /api/provisioning/accounts/{id}/key, body {}
    def release_account(self, external_id: str) -> GristRelease     # 404 "not found" -> GristRelease(False, False)
```

**Requests.**

- It uses its own `requests.Session`, with `timeout=30`, `allow_redirects=False` and headers `Authorization: Bearer <key>` and `Accept: application/json`.
- `__repr__` is `GristProvisionerClient(<host>)`.
- An `external_id` that does not match `^[A-Za-z0-9._@-]{1,200}$` raises `GristProvisionerClientError(f"Not a Grist provisioning id: {external_id!r}")` before any request.

**Parsing.**

- `_detail(response)` is the JSON `error` string cut to 200 characters, or `"no detail"`.
- `_bool(d, key)` is true only for JSON `true`; `_int(d, key)` accepts only an int that is not a bool.
- A 2xx body that is not a JSON object, or that misses a field in the contract → `The Grist provisioner answered with something unexpected.`
- A 201 from `issue_key` with an empty `key` gets the same message.

**Errors.** Each has `status` and `detail` set, and exactly this message:

| Condition | Message |
|---|---|
| `requests.RequestException` | `Could not reach the Grist provisioner at {host}: {type(exc).__name__}` (status `None`) |
| 3xx | `The Grist provisioner answered with a redirect (HTTP {status}). Check --grist-provisioner-url.` |
| 401 | `The Grist provisioner refused the provisioning key (HTTP 401). Check the key in the daemon's environment.` |
| 404 with detail `"no detail"` | `This server is not a Grist provisioner (HTTP 404). Check --grist-provisioner-url.` |
| 404 with another detail, on `upsert_account` | `Grist has no account for this person yet (HTTP 404).` |
| 404 with another detail, on `issue_key` | `The Grist provisioner has no link for this person (HTTP 404).` |
| 404 with another detail, on `release_account` | none: returns `GristRelease(False, False)` |
| 403 | `The Grist provisioner refused this account (HTTP 403): {detail}` |
| 409 | `The Grist provisioner refused this account (HTTP 409): {detail}` |
| 5xx | `The Grist provisioner answered with an error (HTTP {status}).` |
| any other 4xx | `The Grist provisioner refused the request (HTTP {status}): {detail}` |

**Logging.** One info line per call: `Grist provisioning at %s: %s %s` (host, method name, external id). Never an email, key or provisioning key.

### `src/privacyfence/grist_provisioning.py` (new)

**Constants.**

| Name | Value |
|---|---|
| `RENEW_BEFORE_SECONDS` | `30 * 86400` |
| `INACTIVE_AFTER_SECONDS` | `90 * 86400` |
| `SWEEP_INTERVAL_SECONDS` | `6 * 60 * 60` |
| `PACE_SECONDS` | `0.5` |
| `QUEUE_MAX` | `1000` |
| `STATE_FILE` | `"credentials/grist_provisioning.json"` |
| `TOKEN_FILE` | `"credentials/grist_token.json"` (the same path as `daemon_main.TOKEN_FILES["grist"]`; this module does not import `daemon_main`) |

**State file.** `users/<id>/credentials/grist_provisioning.json`, written with `secure_files.atomic_write_json`:

```json
{"email": "a@x.com", "status": "provisioned", "detail": "", "last_sign_in": 1767225600, "updated_at": 1767225600}
```

| Status | Meaning |
|---|---|
| `pending` | A sign-in queued this person; the worker has not finished yet. |
| `provisioned` | PrivacyFence holds a managed key that the provisioner created. |
| `adopted` | PrivacyFence holds the person's own Grist key. It is never revoked by PrivacyFence. |
| `user_connected` | The key the person pasted on `/connect` is kept. The provisioner is not called. |
| `no_account` | Grist has no account for this email yet. The sweep retries. |
| `admin` | The provisioner refused, because this is the Grist install admin. The person connects by hand. |
| `disabled` | The Grist account is disabled. |
| `no_email` | The IdP sent no email. |
| `unverified_email` | The IdP did not say the email is verified, and `trust_unverified_email` is off. |
| `released` | `--grist-release` ran. The email is cleared. |
| `error` | Anything else. `detail` holds the `GristProvisionerClientError` message. |

**Per-principal lock.** `principal_lock(principal_id: str) -> threading.Lock` returns one lock per id, kept in a module dict `_LOCKS` that a module lock guards. `routes_connect.grist_connect` saves the pasted key inside `with grist_provisioning.principal_lock(principal.id):`. The lock is never held across a provisioner call, and never taken twice in a nested way.

**State writes.** Every change to the state file goes through one helper, `_update_state(principal_id: str, *, now: float, **fields: Any) -> dict[str, Any]`. Under `principal_lock(principal_id)` it reads the file (an unreadable or missing file counts as `{}`), applies `fields`, sets `updated_at = int(now)`, writes it with `secure_files.atomic_write_json`, and returns the new state. `_read_state(principal_id) -> dict[str, Any]` reads it the same way without writing. No other code writes the file, so a sign-in and the worker never lose each other's fields.

**`provision_principal(principal_id: str, *, client: GristProvisionerClient, config: GristProvisioningConfig, on_change: Callable[[str], None], now: float) -> str`**

It reads the email from the state file, writes `status` and `detail` through `_update_state` (leaving `email` and `last_sign_in` alone), and returns the status. "A usable manual file" below means: `load_token_file` succeeds, `auth == "api_key"`, `token_source(record) == "manual"` and `record["server_url"] == config.server_url`. "A usable provisioned file" is the same with `token_source == "provisioned"`.

0. `state = _read_state(principal_id)`. If `state["status"] == "released"`, return `released` at once: `--grist-release` ran after this person was queued, and the worker must not provision them again. `email = state.get("email", "")`.
1. If `email` is empty → `no_email`.
2. Under `principal_lock(principal_id)`, read `paths.user_dir(Principal(id=principal_id)) / TOKEN_FILE`. If it is a usable manual file → `user_connected`, and the provisioner is not called.
3. `account = client.upsert_account(principal_id, email)`.
4. If `account.disabled` → `disabled`; under the lock, delete a provisioned file if there is one, and call `on_change` after the lock if it deleted one.
5. Skip the key call only when all of these hold, using the file read in step 2:
   - it is a usable provisioned file;
   - `account.has_key` is true;
   - `key_id(record["api_key"]) == account.key_id`;
   - either `record["managed"]` is false, or `record["expires_at"] - now > RENEW_BEFORE_SECONDS`.

   Otherwise:
   - `issued = client.issue_key(principal_id)`;
   - under the lock, read the file again. If it is now a usable manual file → `user_connected`, and nothing is saved. Else `save_api_key(path, config.server_url, issued.key, source="provisioned", managed=issued.managed, expires_at=issued.expires_at)`;
   - after the lock is released, call `on_change(principal_id)`.
6. Return `provisioned` when the key PrivacyFence now holds is managed, else `adopted`.

On a `GristProvisionerClientError` as `exc`:

| Error | Status, and what else happens |
|---|---|
| `exc.status == 409` and `exc.detail.startswith("account is a Grist admin")` | `admin`; delete a provisioned file under the lock, and call `on_change` if it deleted one |
| `exc.status == 403` and `exc.detail == "account disabled"` | `disabled`; the same deletion |
| `exc.status == 404` and `exc.detail.startswith("no Grist account")` | `no_account` |
| any other | `error`, with `str(exc)` as `detail` |

Nothing is raised. Logging is info `Grist provisioning for %s: %s` (principal id, status); `error` is logged at warning as `Grist provisioning for %s failed: %s`. Never an email, a name or a key.

**`GristProvisioner`**

```python
class GristProvisioner:
    def __init__(self, *, client: GristProvisionerClient, config: org_mode.GristProvisioningConfig,
                 on_change: Callable[[str], None], stop_event: threading.Event,
                 clock: Callable[[], float] = time.time) -> None
    def request(self, principal: Principal, *, email_verified: bool) -> None
    def process_next(self, timeout: float) -> bool
    def sweep_once(self) -> int
    def start(self) -> None
```

- **`request`** never blocks.
  1. Through `_update_state` it writes `email = principal.email`, `last_sign_in = int(clock())`, `detail = ""`, and a status: `no_email` when `principal.email` is empty; `unverified_email` when `email_verified` is false and `config.trust_unverified_email` is false; otherwise `pending`. In the first two cases it stops there. (A sign-in after `--grist-release` thereby replaces `released` with `pending`, so the person is provisioned again.)
  2. Otherwise it enqueues `principal.id`, unless the id is already in the `_queued` set, which a lock guards.
  3. When the queue is full (`queue.Queue(maxsize=QUEUE_MAX)`, `put_nowait`), it logs the warning `Grist provisioning queue is full; %s waits for the next sweep or sign-in` with the id.
- **`process_next(timeout)`**
  1. It takes one id, waiting up to `timeout`, and returns `False` if none came.
  2. It removes the id from `_queued`.
  3. It calls `provision_principal(..., now=clock())`, then `stop_event.wait(PACE_SECONDS)`, and returns `True`.

  It is the only caller of `provision_principal`. Its body is wrapped in `except Exception` → `logger.warning("Grist provisioning worker error: %s", exc)`.
- **`sweep_once`** only enqueues; it never calls the provisioner itself. Its candidates are every `users/<id>/` with a safe id (as in `web/mcp_auth.py:208-220`) whose state status is `pending`, `provisioned`, `adopted`, `error` or `no_account`, and whose `last_sign_in` is within `INACTIVE_AFTER_SECONDS` of `clock()`. It returns how many it enqueued.
- **`start`** starts two threads, both with `daemon=True`:
  - `grist-provisioning` loops `process_next(1.0)` until `stop_event` is set.
  - `grist-provisioning-sweep` first calls `check()` on its **own** `GristProvisionerClient`. On success it logs info `Grist provisioning ready at %s (%d accounts managed)`; on `GristProvisionerClientError` it logs warning `Grist provisioning check failed: %s`. Then it loops `sweep_once()` and `stop_event.wait(SWEEP_INTERVAL_SECONDS)` until the event is set, with each pass wrapped in `except Exception` → warning `Grist provisioning sweep failed (will retry): %s`.

**Module functions**

```python
_INSTANCE: GristProvisioner | None = None

def start(org_config: dict[str, Any], *, on_change: Callable[[str], None], stop_event: threading.Event) -> GristProvisioner | None
def notify_sign_in(principal: Principal, *, email_verified: bool) -> None
def email_verified_claim(claims: dict[str, Any]) -> bool   # claims.get("email_verified") in (True, "true")
def release(principal_id: str) -> None
```

- **`start`.**
  1. `config = GristProvisioningConfig.from_org_config(org_config)`; when it is not enabled, return `None`.
  2. `key = os.environ.get(config.key_env, "").strip()`. When it is empty, log warning `Grist auto-provisioning is on, but %s is not set; people connect Grist by hand on /connect.` (the variable name) and return `None`.
  3. Otherwise build the worker's client and the provisioner, call its `start()`, set `_INSTANCE` and return it.
- **`notify_sign_in`.** It does nothing when `_INSTANCE is None`. Otherwise it calls `_INSTANCE.request(...)` inside `try`/`except Exception` → `logger.warning("Grist provisioning could not take the sign-in of %s: %s", principal.id, exc)`. It never raises.
- **`release`.** First it writes status `released` with `email: ""` and `detail: ""` through `_update_state`. Then, under `principal_lock`, it deletes the token file when it is provisioned (managed or adopted); a manual file stays. `run_grist_release` calls it **before** the provisioner call, so a worker already holding this person sees `released` in step 0 and stops.

### Wiring

- **`daemon_main._start_org_web_server`.**
  - Call `org_mode.GristProvisioningConfig.from_org_config(org_config)` next to the other `from_org_config` calls (`:1158-1159`), so a broken section refuses startup.
  - Right after `connector_registry = ConnectorRegistry(...)` (`:1210`), call `grist_provisioning.start(org_config, on_change=connector_registry.evict, stop_event=_shutdown_event)`.
  - Local mode never calls it.
- **`routes_org_identity.login_callback`.** After `session_id = sessions.create(principal)` (`:227`), outside the `try` block, call `await asyncio.to_thread(grist_provisioning.notify_sign_in, principal, email_verified=grist_provisioning.email_verified_claim(claims))`. It writes a file, so it never runs on the event loop (docs/coding-and-testing-guidelines.md §1.7). If a `peek_provisioning.notify_sign_in` call is already there, put this one right after it.
- **`OrgOAuthProvider.handle_idp_callback`.** Make the same `await asyncio.to_thread(...)` call after `org_identity.check_authz_policy(...)` (`:476`), so people who only use an AI client are provisioned too.
- **`routes_connect.grist_connect`.** Wrap its `save_api_key` call (`:766-768`) in `with grist_provisioning.principal_lock(principal.id):`.
- **`--grist-release PRINCIPAL_ID`.** Add it next to `--revoke-scoped-tokens`, with help `Release the Grist key PrivacyFence holds for the account PRINCIPAL_ID: the provisioner deletes it in Grist if it created it, PrivacyFence forgets it, and the daemon exits. Needs the provisioning key in the environment.` It dispatches to `run_grist_release(principal_id) -> int`. Every message goes to stderr except the success line.
  1. If `safe_principal_id(principal_id) != principal_id`, or `principal_id == "local"`, or `data_dir()/"users"/principal_id` is not a directory → `No such account.`, return 1.
  2. `config = GristProvisioningConfig.from_org_config(load_org_config())`. On `ConfigurationError`, print it and return 1. When it is not enabled → `Grist auto-provisioning is not enabled in the organization config.`, return 1.
  3. When `os.environ.get(config.key_env, "").strip()` is empty → `Set {key_env} to the Grist provisioning key first.`, return 1.
  4. Call `grist_provisioning.release(principal_id)`.
  5. `result = GristProvisionerClient(config.provisioner_url, key).release_account(principal_id)`. On `GristProvisionerClientError` as `exc`, print `{exc} PrivacyFence has already forgotten the key; run --grist-release again to delete it in Grist.` and return 1.
  6. Print to stdout `Released the Grist connection of {principal_id}.`, followed by one of these, and return 0:

     | Result | Added text |
     |---|---|
     | `revoked` | ` The Grist API key PrivacyFence was given is deleted in Grist.` |
     | `released` but not `revoked` | ` Its Grist API key was the person's own, or was already replaced, and stays; delete it in Grist or disable the account there.` |
     | not `released` | ` The Grist provisioner had no link for it.` |
  - A person who signs in again is provisioned again, so release is for people who can no longer sign in. Remove them from the identity provider first. The docs say so.

### `/connect`

- In `_grist_box_html`, when the person is connected, read the token file safely (`load_token_file` inside `try`/`except GristClientError`). When `token_source(record) == "provisioned"`, add `<div class="field-help">Your organization connected your Grist account automatically.</div>` above the form.
- The API-key form stays. A key pasted there is saved as a manual file, which provisioning then leaves alone.
- With a managed key, that key keeps working in Grist until it expires or is released. With an adopted key, the key is the person's own anyway.

### Rejected alternatives (they become ADR text)

- **Forward auth (`GRIST_FORWARD_AUTH_HEADER`) to call `/api/profile/apikey` as each person.** Rejected: in grist-core it is a login system that replaces Grist's own Google sign-in, so the organization would have to move Grist's sign-in behind a proxy such as oauth2-proxy. And any caller that can set the header can act as anyone.
- **A Grist OAuth app.** Not in the free edition.
- **SCIM (`GRIST_ENABLE_SCIM`).** It can create users but issues no credential. Creating accounts was declined: Grist creates them at first sign-in or on sharing, and an empty account reaches nothing. It would also put an admin key into the provisioner.
- **One shared Grist service account.** Rejected: it drops each person's own Grist access rules.
- **The provisioner, or Grist's boot key, inside PrivacyFence.** Rejected: whoever writes Grist's database can act as any Grist user. That power stays in Grist's zone, behind a scoped key that refuses the install admin, as with Peek (ADR on Peek provisioning).
- **Forcing a fresh key for people who already have one.** Rejected: it would break their own scripts. Their key is adopted instead (user decision) and never revoked by PrivacyFence.
- **Rotating managed keys.** Rejected: Grist has one key per person, and a rotation would break anything the person copied it into from Grist's settings page. Instead the provisioner lets a managed key expire 90 days after it was last issued, and PrivacyFence renews it 30 days ahead while the person keeps signing in.
- **Provisioning during sign-in, or provisioning everyone at startup.** Rejected for the same reasons as in the Peek plan: it would make PrivacyFence sign-in wait on Grist, and emails are only known after a sign-in.

## ADRs

Five ADRs, one decision each, taking the next five free numbers when the retirement phase runs (0242-0246 if the Peek plan's 0239-0241 have landed, else 0239-0243). Each index row says "Accepted; relates to 0142, 0143", as 0230 does, so ADRs 0142 and 0143 themselves are not edited.

1. *Grist API keys come from a Grist-side provisioner with a scoped key, and PrivacyFence only ever calls it.* The provisioner in Grist's zone, the key in an environment variable named by the bundle, API-key set-ups only, and the rejected forward-auth, SCIM, service-account and boot-key options.
2. *A Grist key the person pasted on /connect wins over provisioning.* The `source` marker in `grist_token.json` and the per-principal lock shared with `/connect`.
3. *An existing Grist key is adopted and never revoked by PrivacyFence.* One key per person in Grist, the `managed` marker, `key_id`, and why replacing a key was rejected.
4. *Grist provisioning needs a verified email.* `email_verified`, `trust_unverified_email`, and the stored email kept for renewal.
5. *A managed Grist key expires 90 days after it was last issued.* Renewal 30 days ahead, the sweep stopping 90 days after the last sign-in, and therefore a managed key living at most about 150 days after the last sign-in (a renewal near day 60 lasts to about day 150); `--grist-release` for anything sooner.

## Manual steps

Every phase runs against fakes. The end-to-end check needs the provisioner from the companion plan, deployed next to the organization's Grist. That is all in `manual_after`, on the [step-by-step page](https://claude.ai/artifact/5RVxf9Zhi94bU37gLqBcLS):

1. **ma1:** deploy the provisioner next to Grist and create a provisioning key.
2. **ma2:** put the key into PrivacyFence's environment, rebuild the bundle with `--grist-auto-provision`, and restart.
3. **ma3:** check a person with no Grist key, a person with their own key, the install admin, and someone with no Grist account.
4. **ma4:** check independence: the provisioner or Grist down at sign-in.
5. **ma5:** check `--grist-release`, and that the key disappears in Grist.

The companion plan has its own `manual_before`: create the repository, and add its devflow profile from that plan. Those steps are on the same page and are not needed for this plan's phases.

## Risks and open questions

- **The Peek plan landed first.** In that case `org_mode.py` already has `import re` and `_ENV_NAME_RE`; reuse them and do not define them twice. Both `notify_sign_in` calls go in the callbacks, Peek's first. The ADR numbers move as the ADRs section says. Any other overlap you cannot resolve this way → stop with `status=blocked`.
- **An existing test in `tests/unit/test_grist_auth.py` or `TestGristApiKey` breaks** because a manual file is no longer byte-identical. That means `save_api_key` changed more than the Design allows. Fix the code, never the test; if the test itself is wrong, stop with `status=blocked`.
- **`_grist_url` rejects a provisioner URL with a path.** It mirrors `normalize_server_url`, which allows a path prefix but no `/api` segment; the provisioner is documented at a bare host. If a test needs a path, use one without `api`.
- **`/dod` flags the QA-fixture row** for `src/privacyfence/*_client.py`. This plan adds no file matching that pattern; if a phase does, it has deviated from the Design. Stop and say so.
- **The companion plan changes the wire contract** (a field name, a status, an error string). This plan is the source of the contract: the companion follows it. A phase that finds a mismatch in the companion's repository stops with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: grist-auto-provisioning
feature_branch: feature/grist-auto-provisioning
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/5RVxf9Zhi94bU37gLqBcLS
manual_steps_source: docs/grist-auto-provisioning-plan-manual-steps.html
manual_before:
  - id: mb1-companion-plan
    title: Create privacyfence/grist-provisioner and copy the companion plan there (step-by-step page, item 1)
    why: Phase p6 deletes docs/grist-provisioner-companion-plan.md from this repository; it must exist in the provisioner repository first, or it is lost.
    done_when: docs/grist-provisioner-plan.md exists on branch plan/grist-provisioner in privacyfence/grist-provisioner
manual_after:
  - id: ma1-provisioner
    title: Deploy the Grist provisioner next to Grist (companion plan) and create a provisioning key
    why: The phases test against fakes; only the real provisioner on the real Grist database proves the wire contract.
  - id: ma2-pf-config
    title: Put the key into the PrivacyFence daemon's environment, rebuild the bundle with --grist-auto-provision, restart
    why: Exercises the env-var key, bundle validation and the startup check on a real install.
  - id: ma3-four-people
    title: Sign in as a person with no Grist key, one with their own key, the Grist install admin, and one with no Grist account
    why: Verifies managed, adopted, the admin refusal and no_account against the real provisioner, Grist and Google.
  - id: ma4-independence
    title: Stop the provisioner and sign in to PrivacyFence; stop Grist and sign in again
    why: Proves sign-in never waits on Grist, which CI cannot show across separate servers.
  - id: ma5-release
    title: Run privacyfence-app --grist-release for the managed test person and check the key is gone in Grist
    why: Verifies offboarding and revocation against the real Grist database.
verify_after_merge:
  - python3 -m pytest tests/unit/test_org_mode.py tests/unit/test_build_org_bundle.py tests/unit/test_grist_auth.py tests/unit/test_grist_provisioner_api.py tests/unit/test_docs_configuration_reference.py tests/unit/test_systemic_gate_invariants.py -q
  - python3 -m pytest tests/unit/test_grist_provisioning.py tests/unit/test_daemon_main.py tests/unit/web/test_routes_connect.py tests/unit/web/test_routes_org_identity.py tests/unit/web/test_oauth_provider.py -q
final_checks:
  - docs/grist-auto-provisioning-plan.md, docs/grist-auto-provisioning-plan-manual-steps.html and docs/grist-provisioner-companion-plan.md are deleted and nothing links to them
  - the five Grist provisioning ADRs exist, are Accepted, and are rows in docs/adr/README.md
  - CHANGELOG.md has the Grist auto-provisioning entry under "## [Unreleased]" and no new version heading
  - the PR description links the companion plan in privacyfence/grist-provisioner (manual_before mb1-companion-plan)
phases:
  - id: p1-config
    title: GristProvisioningConfig and the bundle builder flags
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/org_mode.py
      - scripts/build_org_bundle.py
      - docs/configuration-reference.md
      - tests/unit/test_org_mode.py
      - tests/unit/test_build_org_bundle.py
    brief: |
      Must-read docs: CLAUDE.md, CONTRIBUTING.md, docs/releasing.md, docs/coding-and-testing-guidelines.md, docs/testing-policy.md and docs/adr/README.md. Then read this plan's Design section "Organization config", including "Builder", and "Risks" (the Peek-plan overlap).
      1. src/privacyfence/org_mode.py:
         - Add `import re` (unless already there) and `from .grist_auth import GristClientError, normalize_server_url`. If importing grist_auth from org_mode causes an import cycle, stop with status=blocked.
         - Add DEFAULT_GRIST_PROVISIONING_KEY_ENV, _ENV_NAME_RE (unless already there) and GristProvisioningConfig after WebPushConfig (:491-514), with the checks in the order and with the messages Design gives, verbatim.
         - Add "DEFAULT_GRIST_PROVISIONING_KEY_ENV" and "GristProvisioningConfig" to __all__ (:538-559).
      2. scripts/build_org_bundle.py:
         - Add the four flags to the Grist group (:284), with the help texts from Design.
         - At :697-705, carry the old auto_provision into grist_section, as Design says.
         - Add the rules, in Design's order and with the SystemExit messages verbatim, right after the --web-push check (:840-843).
         - Add the --mode local pop at :765-773.
         - Add the _DEFAULT_GRIST_PROVISIONING_KEY_ENV module constant next to _grist_url.
      3. docs/configuration-reference.md, after the --grist-auth-server-url row (:289), add four rows in that table's columns (option | default | bundle key | meaning):
         - `--grist-auto-provision` / `--no-grist-auto-provision` | not written (off) | `grist.auto_provision.enabled` | the flag's help text
         - `--grist-provisioner-url URL` | none | `grist.auto_provision.provisioner_url` | its help text
         - `--grist-provisioning-key-env NAME` | `PRIVACYFENCE_GRIST_PROVISIONING_KEY` | `grist.auto_provision.key_env` | its help text
         - `--grist-trust-unverified-email` | off | `grist.auto_provision.trust_unverified_email` | its help text
      4. Tests.
         - tests/unit/test_org_mode.py: add class TestGristProvisioningConfigFromOrgConfig with these tests:
           - test_absent_is_disabled
           - test_enabled_with_server_and_provisioner (default key_env, both URLs normalised)
           - test_custom_key_env
           - test_trust_unverified_email
           - test_non_bool_enabled
           - test_non_bool_trust_unverified_email
           - test_bad_key_env, parametrised over "lower", "1ABC" and "A-B"
           - test_disabled_skips_url_checks ({"enabled": false} in local mode is fine)
           - test_enabled_in_local_mode
           - test_enabled_without_server_url
           - test_enabled_with_oauth_app
           - test_enabled_without_provisioner_url
           - test_bad_server_url ("http://grist.example.com")
           - test_bad_provisioner_url ("http://prov.example.com")
           Each test that expects an error asserts the exact message.
         - tests/unit/test_build_org_bundle.py: add class TestGristAutoProvisionFlags, in the style of TestWebPushFlag, with these tests:
           - test_writes_section_with_default_env
           - test_writes_custom_env
           - test_writes_trust_unverified_email
           - test_no_flag_writes_enabled_false
           - test_refuses_without_mode_org
           - test_refuses_without_server_url
           - test_refuses_with_oauth_app
           - test_refuses_without_provisioner_url
           - test_refuses_companion_flags_alone, parametrised over the three companion flags
           - test_mode_local_strips_it
           - test_merge_new_server_url_keeps_auto_provision
           - test_merge_reuses_existing_provisioner_url
           - test_fresh_org_build_succeeds
      5. Run ruff check . and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_org_mode.py -q --cov=privacyfence.org_mode --cov-branch --cov-report=term-missing passes and reports 100% for org_mode.py
      - python3 -m pytest tests/unit/test_build_org_bundle.py -k GristAutoProvision -q passes
      - python3 -m pytest tests/unit/test_docs_configuration_reference.py -q passes
      - grep -n "class GristProvisioningConfig" src/privacyfence/org_mode.py matches
      - ruff check . passes

  - id: p2-api-client
    title: Credential file source marker and GristProvisionerClient
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/grist_auth.py
      - src/privacyfence/grist_provisioner_api.py
      - tests/unit/test_grist_auth.py
      - tests/unit/test_grist_provisioner_api.py
    brief: |
      Must-read docs as in p1. Read this plan's Design sections "The provisioner's wire contract", "The credential file (grist_auth)" and "src/privacyfence/grist_provisioner_api.py", and "Risks".
      1. src/privacyfence/grist_auth.py:
         - Change save_api_key (:162-166) and load_token_file (:169-189) exactly as Design says, and add token_source and key_id (add `import hashlib`).
         - save_token_file stays the one write site, and a manual file stays byte-identical.
      2. Create src/privacyfence/grist_provisioner_api.py with GristProvisionerClientError, GristProvisionedAccount, GristIssuedKey, GristRelease and GristProvisionerClient. Use the signatures, paths, bodies, parsing rules, error messages and log line in Design, verbatim. Model the request plumbing on peek_client._send (src/privacyfence/peek_client.py:112): its own requests.Session, no redirects, a 30 s timeout.
      3. Tests.
         - tests/unit/test_grist_auth.py: add these tests to TestTokenFile (:488):
           - test_manual_api_key_file_is_unchanged (the JSON has exactly auth, server_url and api_key)
           - test_provisioned_file_round_trips
           - test_unknown_source_is_unreadable
           - test_provisioned_non_bool_managed_is_unreadable
           - test_provisioned_bool_expires_at_is_unreadable
           - test_token_source
           - test_key_id_is_sha256_prefix
         - tests/unit/test_grist_provisioner_api.py (new): mock requests.Session.request with monkeypatch, as tests/unit/test_peek_client.py does. Tests:
           - test_check_returns_accounts
           - test_upsert_sends_put_with_email_and_bearer
           - test_upsert_parses_account
           - test_upsert_missing_field_is_unexpected
           - test_issue_key_parses_and_hides_key_in_repr
           - test_issue_key_without_key_is_unexpected
           - test_release_returns_flags
           - test_release_404_not_found_is_false_false
           - test_plain_404_is_not_a_provisioner, for check, upsert, issue_key and release
           - test_json_404_on_upsert_is_no_account
           - test_json_404_on_issue_key_is_no_link
           - test_401_message
           - test_403_and_409_carry_detail
           - test_5xx_message
           - test_other_4xx_message
           - test_redirect_is_refused
           - test_transport_error_message
           - test_invalid_external_id_is_refused_before_any_request
           - test_repr_has_host_not_key
           - test_allow_redirects_false_and_timeout
           - test_log_lines_carry_no_email_or_key (caplog)
      4. Run ruff check ., python3 -m mypy src/privacyfence and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_grist_provisioner_api.py -q --cov=privacyfence.grist_provisioner_api --cov-branch --cov-report=term-missing passes and reports 100% for grist_provisioner_api.py
      - python3 -m pytest tests/unit/test_grist_auth.py -q passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m mypy src/privacyfence passes
      - ruff check . passes

  - id: p3-provisioner
    title: grist_provisioning module (per-person flow, single worker, sweep)
    depends_on: [p1-config, p2-api-client]
    complexity: M
    touches:
      - src/privacyfence/grist_provisioning.py
      - tests/unit/test_grist_provisioning.py
      - tests/conftest.py
      - scripts/check_coverage_floor.py
    brief: |
      Must-read docs as in p1. Read this plan's Design section "src/privacyfence/grist_provisioning.py" in full. The pattern for a guarded loop is daemon_main._run_personal_data_sweep (src/privacyfence/daemon_main.py:311-322).
      1. Create src/privacyfence/grist_provisioning.py with exactly what Design describes:
         - the constants and the state file;
         - principal_lock;
         - _update_state and _read_state;
         - provision_principal, with steps 0-6 and its error table;
         - GristProvisioner, with request, process_next, sweep_once and start;
         - _INSTANCE, start, notify_sign_in, email_verified_claim and release.
         Rules:
         - Write every file with secure_files.atomic_write_json, and the token file only through grist_auth.save_api_key.
         - Every thread has daemon=True and exits once stop_event is set.
         - Only process_next calls provision_principal.
      2. tests/conftest.py _reset() (:90-145): add `grist_provisioning._INSTANCE = None` and `grist_provisioning._LOCKS.clear()`. Add grist_provisioning to the import list at :27-46.
      3. scripts/check_coverage_floor.py: add "src/privacyfence/grist_provisioning.py": 100.0 and "src/privacyfence/grist_provisioner_api.py": 100.0 in the "# Org-config fail-closed load path." group, right after the daemon_main.py line.
      4. Tests in tests/unit/test_grist_provisioning.py.
         Setup:
         - Use tmp_path as the data dir: monkeypatch paths.data_dir as tests/unit/test_principal_isolation.py does.
         - A FakeProvisionerClient records calls and can be set to raise GristProvisionerClientError(..., status=..., detail=...). No network.
         Tests:
         - test_no_email_writes_no_email
         - test_released_status_skips_provisioning (state released → returns released, the fake records no call)
         - test_update_state_keeps_other_fields_and_sets_updated_at
         - test_manual_file_for_same_server_is_kept_and_provisioner_not_called
         - test_manual_file_for_other_server_is_replaced
         - test_new_key_is_issued_saved_managed_and_evicted (source provisioned, managed true and expires_at in the file; on_change called once; status provisioned)
         - test_existing_key_is_adopted (managed false; status adopted)
         - test_fresh_managed_key_is_not_reissued
         - test_managed_key_within_30_days_is_reissued
         - test_adopted_key_is_not_reissued_while_key_id_matches
         - test_changed_key_id_is_reissued (the person made a new key in Grist)
         - test_has_key_false_is_reissued
         - test_disabled_account_removes_provisioned_file
         - test_disabled_403_on_issue_removes_provisioned_file
         - test_admin_409_writes_admin_and_removes_provisioned_file
         - test_no_account_404_writes_no_account
         - test_other_error_writes_error_with_message
         - test_manual_save_during_issue_wins (the fake's issue_key writes a manual file before returning; it stays and the status is user_connected)
         - test_log_lines_carry_no_email_or_key (caplog)
         - test_request_unverified_email_is_not_queued
         - test_request_unverified_email_is_queued_when_trusted
         - test_request_no_email_is_not_queued
         - test_request_records_last_sign_in_and_sets_pending
         - test_sign_in_after_release_sets_pending_and_provisions
         - test_request_is_deduplicated_while_queued
         - test_full_queue_drops_with_warning
         - test_process_next_returns_false_on_empty_queue
         - test_process_next_paces_with_stop_event_wait
         - test_process_next_logs_unexpected_errors
         - test_sweep_enqueues_recent_provisioned_adopted_error_and_no_account_only (not admin, disabled, user_connected, no_email, unverified_email or released, and not those older than 90 days)
         - test_start_returns_none_when_disabled
         - test_start_returns_none_and_warns_without_key (exact message)
         - test_start_logs_ready_and_check_failed (both messages)
         - test_notify_sign_in_is_noop_without_instance
         - test_notify_sign_in_never_raises
         - test_email_verified_claim, parametrised over True, "true", False, "false", absent and 1
         - test_release_removes_provisioned_file_keeps_manual_file_and_clears_email
         Drive threads deterministically: call process_next and sweep_once directly, and for start() set stop_event and join. No sleep longer than 0.1 s.
      5. Run ruff check ., python3 -m mypy src/privacyfence and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_grist_provisioning.py -q --cov=privacyfence.grist_provisioning --cov-branch --cov-report=term-missing passes and reports 100% for grist_provisioning.py
      - grep -n "grist_provisioning._INSTANCE = None" tests/conftest.py matches
      - grep -n "grist_provisioning.py" scripts/check_coverage_floor.py matches
      - python3 -m mypy src/privacyfence passes
      - ruff check . passes

  - id: p4-wiring
    title: Start the provisioner, hook both sign-in callbacks, add --grist-release
    depends_on: [p3-provisioner]
    complexity: M
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/web/routes_org_identity.py
      - src/privacyfence/web/oauth_provider.py
      - tests/unit/test_daemon_main.py
      - tests/unit/web/test_routes_org_identity.py
      - tests/unit/web/test_oauth_provider.py
      - docs/configuration-reference.md
    brief: |
      Must-read docs as in p1. Read this plan's Design section "Wiring", and "Risks" (the Peek-plan overlap).
      1. In daemon_main._start_org_web_server (src/privacyfence/daemon_main.py:1109-1265), add the validation and the start call exactly as Design says.
      2. In web/routes_org_identity.py login_callback, add the `await asyncio.to_thread(grist_provisioning.notify_sign_in, ...)` call after sessions.create (:227), outside the try block at :213-224, exactly as Design says.
      3. In web/oauth_provider.py handle_idp_callback, add the same `await asyncio.to_thread(...)` call after check_authz_policy (:476).
      4. In daemon_main:
         - Add --grist-release next to --revoke-scoped-tokens (:2405), and dispatch it next to :2497.
         - Implement run_grist_release exactly as Design steps 1-6 say, with the messages verbatim and on the streams given.
         - Put it after run_revoke_scoped_tokens, under a "# --grist-release" banner comment in the same style.
      5. docs/configuration-reference.md: in the daemon option table (:357-368), add a row after --revoke-scoped-tokens:
         | `--grist-release PRINCIPAL_ID` | Release the Grist key PrivacyFence holds for that account: the provisioner deletes it in Grist if it created it, and PrivacyFence forgets it. Then the daemon exits. Needs the Grist provisioning key in the environment; run it as the account the daemon runs as. |
      6. Tests.
         - tests/unit/test_daemon_main.py:
           - test_org_server_starts_grist_provisioner_with_registry_evict (monkeypatch grist_provisioning.start and assert its arguments)
           - test_org_server_refuses_broken_grist_auto_provision
           - class TestGristRelease, against a fake GristProvisionerClient, with these tests:
             - test_unknown_account
             - test_local_is_refused
             - test_configuration_error_is_printed
             - test_not_enabled
             - test_missing_key
             - test_revoked_message
             - test_own_key_message
             - test_no_link_message
             - test_client_error_exits_1 (the message ends with "run --grist-release again to delete it in Grist.")
             - test_release_runs_before_the_provisioner_call (record the call order)
         - tests/unit/web/test_routes_org_identity.py:
           - test_login_callback_calls_grist_notify_sign_in_with_email_verified (monkeypatch asyncio.to_thread with a recorder and assert it received grist_provisioning.notify_sign_in)
           - test_failed_login_callback_does_not_call_grist_notify_sign_in
         - tests/unit/web/test_oauth_provider.py:
           - test_handle_idp_callback_calls_grist_notify_sign_in
           - test_authorization_denied_does_not_call_grist_notify_sign_in
      7. Run ruff check ., python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py -k "grist_provisioner or GristRelease or grist_auto_provision" -q passes
      - python3 -m pytest tests/unit/web/test_routes_org_identity.py -k grist_notify_sign_in -q passes
      - python3 -m pytest tests/unit/web/test_oauth_provider.py -k grist_notify_sign_in -q passes
      - python3 -m pytest tests/unit -q --cov=privacyfence --cov-branch --cov-report=json:coverage.json && python3 scripts/check_coverage_floor.py coverage.json passes
      - python3 -m pytest tests/unit/test_docs_configuration_reference.py -q passes
      - python3 scripts/mypy_strict_modules.py passes

  - id: p5-connect-and-docs
    title: /connect lock and hint, and the user-facing docs
    depends_on: [p3-provisioner]
    complexity: S
    touches:
      - src/privacyfence/web/routes_connect.py
      - tests/unit/web/test_routes_connect.py
      - docs/grist-setup.md
      - docs/org-mode-setup-guide.md
      - docs/org-mode-docker.md
      - docs/security-and-compliance.md
    brief: |
      Must-read docs as in p1. Read this plan's Design sections "/connect", "Wiring" (the grist_connect and --grist-release bullets), "Organization config" and "The provisioner's wire contract".
      1. web/routes_connect.py:
         - In grist_connect, wrap the save_api_key call (:766-768) in `with grist_provisioning.principal_lock(principal.id):`.
         - In _grist_box_html (:1000-1028), add the exact field-help line from Design when connected and the token file's source is provisioned.
      2. tests/unit/web/test_routes_connect.py:
         - TestGristBox (:953): a provisioned file shows the line; a manual file does not.
         - TestGristApiKey (:1145): grist_connect takes the principal lock (monkeypatch principal_lock with a recording lock).
      3. docs/grist-setup.md: add "## Automatic API keys (organization mode)" after "## Connect with an API key". Cover:
         - what it does, that it needs a self-hosted Grist and the Grist provisioner (link to the provisioner repository's README, named in the companion plan), and that the provisioner never connects to PrivacyFence;
         - storing the key in the daemon's environment as PRIVACYFENCE_GRIST_PROVISIONING_KEY (or the --grist-provisioning-key-env name): the systemd EnvironmentFile and the Docker privacyfence.env;
         - --grist-auto-provision and --grist-provisioner-url, and that it works only without a Grist OAuth app;
         - the verified-email rule, --grist-trust-unverified-email, and when that is safe;
         - that a key pasted on /connect wins;
         - managed keys versus adopted keys: Grist has one key per person, an existing key is adopted and never deleted by PrivacyFence, and regenerating or removing the key in Grist disconnects PrivacyFence until the next sign-in;
         - that the install admin is never provisioned and connects by hand;
         - that people without a Grist account are provisioned once Grist has one (first sign-in to Grist, or a share);
         - managed keys expire 90 days after they were last issued and are renewed 30 days ahead while the person signs in, so a managed key lives at most about 150 days after the last sign-in; --grist-release for anything sooner;
         - --grist-release and "remove them from the identity provider first"; and that a running daemon keeps a person's Grist connector in memory until it is rebuilt, so restart the daemon after --grist-release when the key was adopted (a deleted managed key stops working in Grist at once);
         - what each product does when the other is down.
         Add to "## Troubleshooting" every new message in Design, each with its fix: the startup warnings, the ready and check-failed lines, the GristProvisionerClient errors, the --grist-release messages and the queue-full warning. Add the new bundle flags to "## Build and distribute the bundle".
      4. docs/org-mode-setup-guide.md:
         - In the connector table's Grist row (:238), add "or automatic API keys with --grist-auto-provision ([Grist setup](grist-setup.md#automatic-api-keys-organization-mode))".
         - In the bundle option table (:337-339), add the four new options.
         - Change the systemd unit comment (:658) to "For --audit-forwarding-http-bearer-token-env and the Grist provisioning key; …".
         - In the users/<principal> table (:1061-1066), add a row: `users/<principal>/credentials/grist_provisioning.json` | "Grist auto-provisioning state and the person's email, for key renewal."
      5. docs/org-mode-docker.md:
         - Next to :169, add the same note for the Grist provisioning key in privacyfence.env.
         - Next to :206, add `docker compose exec privacyfence privacyfence-app --grist-release PRINCIPAL_ID`.
      6. docs/security-and-compliance.md: in the offboarding text at :262-263, add running --grist-release when Grist auto-provisioning is on, and that an adopted key stays in Grist.
      7. Run ruff check . and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_connect.py -k Grist -q passes
      - grep -n "## Automatic API keys (organization mode)" docs/grist-setup.md matches
      - grep -n "grist_provisioning.json" docs/org-mode-setup-guide.md matches
      - grep -n "grist-release" docs/security-and-compliance.md docs/org-mode-docker.md matches in both files

  - id: p6-retire
    title: ADRs, changelog, delete the plan documents
    depends_on: [p4-wiring, p5-connect-and-docs]
    complexity: S
    touches:
      - docs/adr/*grist*.md
      - docs/adr/README.md
      - CHANGELOG.md
      - docs/grist-auto-provisioning-plan.md
      - docs/grist-auto-provisioning-plan-manual-steps.html
      - docs/grist-provisioner-companion-plan.md
    brief: |
      Read docs/adr/README.md (template, rules, one decision per ADR). Take the next five free ADR numbers on this branch as it stands.
      1. Write the five ADRs from this plan's ADRs section, in that order, each with Status Accepted, named:
         - docs/adr/NNNN-grist-api-keys-come-from-a-grist-side-provisioner.md
         - docs/adr/NNNN-a-pasted-grist-key-wins-over-provisioning.md
         - docs/adr/NNNN-an-existing-grist-key-is-adopted-and-never-revoked.md
         - docs/adr/NNNN-grist-provisioning-needs-a-verified-email.md
         - docs/adr/NNNN-a-managed-grist-key-expires-90-days-after-it-was-issued.md
         Each has these sections:
         - Context: ADR 0142, ADR 0143, the free edition with no OAuth apps, one key per person in Grist, and Grist in its own zone;
         - Decision;
         - Consequences;
         - the matching rejected alternatives from Design "Rejected alternatives".
         Link source files (src/privacyfence/grist_provisioning.py, grist_provisioner_api.py, grist_auth.py, org_mode.py), never this plan.
      2. Add the five rows to docs/adr/README.md in the existing format, each with status "Accepted; relates to 0142, 0143". Do not edit ADRs 0142 or 0143.
      3. CHANGELOG.md: under "## [Unreleased]", add "### Added" (or reuse it if present) with one entry in the file's bold-lead style:
         "- **Grist API keys for everyone, in organization mode.** With a self-hosted Grist, the Grist provisioner and `--grist-auto-provision`, each person who signs in and has a Grist account gets their Grist connection set up for them; a key they paste on /connect still wins, an existing Grist key is reused rather than replaced, and `privacyfence-app --grist-release` deletes the key PrivacyFence was given when someone leaves."
         Never add a version heading.
      4. git rm docs/grist-auto-provisioning-plan.md docs/grist-auto-provisioning-plan-manual-steps.html docs/grist-provisioner-companion-plan.md, then grep that nothing links to them.
      5. Run ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - test ! -e docs/grist-auto-provisioning-plan.md && test ! -e docs/grist-auto-provisioning-plan-manual-steps.html && test ! -e docs/grist-provisioner-companion-plan.md
      - grep -rn "grist-auto-provisioning-plan\|grist-provisioner-companion-plan" docs src README.md CHANGELOG.md prints nothing
      - ls docs/adr/*-grist-api-keys-come-from-a-grist-side-provisioner.md docs/adr/*-a-pasted-grist-key-wins-over-provisioning.md docs/adr/*-an-existing-grist-key-is-adopted-and-never-revoked.md docs/adr/*-grist-provisioning-needs-a-verified-email.md docs/adr/*-a-managed-grist-key-expires-90-days-after-it-was-issued.md lists five files
      - grep -c "grist-api-keys-come-from-a-grist-side-provisioner\|a-pasted-grist-key-wins-over-provisioning\|an-existing-grist-key-is-adopted\|grist-provisioning-needs-a-verified-email\|a-managed-grist-key-expires" docs/adr/README.md prints 5
      - python3 -m pytest tests/unit -q passes
```
