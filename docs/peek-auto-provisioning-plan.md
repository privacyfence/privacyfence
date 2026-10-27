# Peek auto-provisioning in organization mode — plan

## Goal

Today, organization mode offers Peek on `/connect`, and each person connects it by hand with Peek's device sign-in. With this change, an organization can turn on **Peek auto-provisioning** in its bundle. Each person who signs in to PrivacyFence with a verified email then gets a Peek account on the organization's Peek server, or keeps their existing non-admin one, and that account is registered as their Peek connection with no clicks.

PrivacyFence uses a scoped **provisioning key** that a Peek admin creates. All calls go from PrivacyFence to Peek; Peek never connects to PrivacyFence, because the two run in separate DMZs.

Each product keeps working without the other:

- PrivacyFence sign-in never waits on Peek, and a Peek outage only delays provisioning.
- Peek keeps its own users, including people who are not PrivacyFence users, and its own sign-in.

A person's own device sign-in always wins over provisioning. An administrator can release a departing person's Peek connection with one command.

The Peek side is the companion plan `docs/plans/pf-provisioning-plan.md` on `plan/pf-provisioning` in `andras-tkcs/peek`. This plan builds against its wire contract (restated in "Peek's provisioning API" below) and against fakes in tests. No issue tracks it.

## Current state

- **Org config.**
  - `org_config.json` is loaded by `load_org_config` (`src/privacyfence/daemon_main.py:451-529`). Its errors are `org_mode.ConfigurationError` (`:486-528`).
  - The Peek section has only `peek.base_url` (written by `--peek-base-url`) and `peek.enabled` (written by `--enable-peek`, local mode only). See `scripts/build_org_bundle.py:302-307`, `:314-318` and `:714-717`. Note that `:714-715` assigns `bundle["peek"] = {...}`, which replaces the whole section.
  - `peek_client.bundle_base_url` (`src/privacyfence/peek_client.py:86-97`) normalises the address and raises `PeekClientError` for a bad one.
  - Pattern for strict flag validation: `WebPushConfig.from_org_config` (`src/privacyfence/org_mode.py:491-514`). `org_mode.py` does not import `re` yet.
  - Precedent for a secret held in an environment variable named by the bundle: `AuditForwardingConfig.http_bearer_token_env` (`org_mode.py:382-389`).
  - Pattern for an org-only `BooleanOptionalAction`: the builder's `--web-push` (`build_org_bundle.py:533`, with its mode check at `:840-843`, after `bundle["mode"]` is set at `:746`).
  - `--mode local` strips org-only sections (`build_org_bundle.py:765-773`).
  - `tests/unit/test_docs_configuration_reference.py` requires every builder option in `docs/configuration-reference.md`. The Peek rows are at `:283` and `:285`; the daemon option table is at `:357-368`.
- **Principals.**
  - `Principal(id, email, display_name, is_admin)` is defined at `src/privacyfence/principal.py:46-65`.
  - `org_identity.principal_from_claims` (`src/privacyfence/org_identity.py:279-299`) builds it at OIDC sign-in. It copies `claims["email"]` without looking at `email_verified`, and `display_name` falls back to the email or the `sub` (`:291`).
  - The ids match `^[A-Za-z0-9._@-]{1,200}$` (`paths.py:46`), the same as Peek's `external_id`.
  - Two sign-in callbacks have both the principal and the claims:
    - the browser `login_callback` (`src/privacyfence/web/routes_org_identity.py:200-238`, policy check at `:223`, session at `:227`);
    - the MCP-client `OrgOAuthProvider.handle_idp_callback` (`src/privacyfence/web/oauth_provider.py:441-488`, policy check at `:476`).
- **Per-person storage.** Data lives under `users/<principal id>/` (`paths.user_dir`, `src/privacyfence/paths.py:211-234`). `web/mcp_auth.py:208-220` enumerates the directories. Email addresses are not persisted per person today.
- **Peek credential.**
  - `credentials/peek_token.json` holds `{"base_url", "token"}` (`daemon_main.py:213`).
  - Written by `peek_client.save_token_file` (`peek_client.py:261-262`), a registered write site (`tests/unit/test_systemic_gate_invariants.py:191`). Read by `load_token_file` (`:265-284`); `resolve_server` is at `:291-297`.
  - `_send` (`:112`) maps statuses to messages; `_detail` (`:103-109`) returns `"no detail"` for a non-JSON body.
  - The connector is built in `build_connectors` (`daemon_main.py:1694-1709`).
  - The `/connect` device sign-in (`web/routes_connect.py:676-743`) saves the file at `:726-730` and calls `connector_registry.evict(principal.id)`.
  - The box is drawn by `_peek_box_html` (`:960-997`).
- **Background work.** `_run_personal_data_sweep` (`daemon_main.py:311-322`) is the loop pattern and is started at `:2192-2194`. `_start_org_web_server` (`daemon_main.py:1109-1265`) creates `connector_registry` at `:1210`. `ConnectorRegistry.evict` takes its own lock (`connector_registry.py:134-140`).
- **Offboarding precedent.** `--revoke-scoped-tokens PRINCIPAL_ID` (`run_revoke_scoped_tokens`, `daemon_main.py:2323-2346`; argument at `:2405`, dispatch at `:2497`). It is documented at `docs/configuration-reference.md:368`, `docs/security-and-compliance.md:262` and `docs/org-mode-docker.md:206`.
- **Coverage floors.** `scripts/check_coverage_floor.py:88-103` sets these floors: `org_mode.py` 100.0, `daemon_main.py` 96.0, `oauth_provider.py` 99.0.
- **Module state resets.** `tests/conftest.py` `_reset()` (`:90-145`).
- **Changelog.** `## [Unreleased]` is empty (`CHANGELOG.md:44`). Entries go under `### Added` and start with a bold lead sentence.
- **Peek server.** It has no provisioning API until the companion plan lands. Its mux answers unknown routes with HTTP 404 and a plain-text body.

## Design

### Peek's provisioning API (the wire contract, from the companion plan)

All requests carry `Authorization: Bearer <provisioning key>`. The key starts with `peekprov_`. `{external_id}` is the PrivacyFence principal id. Every error answer is JSON `{"error": "…"}`. 429 is the global rate limit.

| Call | Body | Success | Errors PrivacyFence handles |
|---|---|---|---|
| `GET /api/provisioning/self` | none | 200 `{"name", "accounts"}` | 401; 404 with a non-JSON body (no provisioning API) |
| `PUT /api/provisioning/accounts/{external_id}` | `{"email", "name"}`, with `name` at most 100 characters | 200 `{"external_id", "account_id", "email", "name", "created", "adopted", "disabled", "has_token", "token_expires_at"}` | 400; 401; 403 `account email domain is not allowed`; 404 non-JSON (no API); 409 `account is a Peek admin; connect it by hand`; 409 `account is linked to another external id or email` |
| `POST /api/provisioning/accounts/{external_id}/token` | `{"expires_days": 90}` | 201 `{"token", "token_id", "expires_at"}` (unix seconds) | 403 `account disabled`; 403 domain; 404 `not found`; 409 admin |
| `DELETE /api/provisioning/accounts/{external_id}` | none | 200 `{"released": true}` | 404 `not found` (no link); 404 non-JSON (no API) |

The minted token is an ordinary Peek bearer token for that person's account, so every existing `PeekClient` call works with it. Peek answers 403 `account is a Peek admin; connect it by hand` to that token once the account is promoted to admin.

### Organization config

```json
"peek": {
  "base_url": "https://peek.example.com",
  "auto_provision": {"enabled": true, "key_env": "PRIVACYFENCE_PEEK_PROVISIONING_KEY", "trust_unverified_email": false}
}
```

Add to `src/privacyfence/org_mode.py`, after `WebPushConfig`, with `import re` and the names in `__all__`:

```python
DEFAULT_PEEK_PROVISIONING_KEY_ENV = "PRIVACYFENCE_PEEK_PROVISIONING_KEY"
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")

@dataclass(frozen=True)
class PeekProvisioningConfig:
    enabled: bool = False
    key_env: str = DEFAULT_PEEK_PROVISIONING_KEY_ENV
    trust_unverified_email: bool = False
    base_url: str = ""          # normalised; set only when enabled

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "PeekProvisioningConfig": ...
```

`from_org_config` reads `peek` (a dict, else `{}`), then `auto_provision` (a dict, else `{}`). With no section it returns `PeekProvisioningConfig()`. Each of these raises `ConfigurationError` with exactly this message:

- `enabled` or `trust_unverified_email` present and not a bool: `org_config.json's "peek"."auto_provision"."<key>" must be true or false, got {value!r}`
- `key_env` present and not fully matching `_ENV_NAME_RE`: `org_config.json's "peek"."auto_provision"."key_env" must be an environment variable name, got {value!r}`
- enabled, but `resolve_mode(org_config) != "org"`: `org_config.json's "peek"."auto_provision" needs "mode": "org"`
- enabled, but `peek.base_url` is empty: `org_config.json's "peek"."auto_provision" needs "peek"."base_url"`
- enabled, but `peek_client.bundle_base_url(peek, org_mode=True)` raises `PeekClientError` as `exc`: `org_config.json's "peek"."base_url" is not usable: {exc}`. The normalised URL goes into `base_url`.

The key itself is never in the bundle, for the same reason as `http_bearer_token_env`. The daemon reads `os.environ[key_env]`.

**Verified email.** By default only a principal whose ID token says `email_verified` is true (the JSON `true` or the string `"true"`) is provisioned. `trust_unverified_email: true` is for providers that never send the claim but only issue verified, organization-owned addresses (Microsoft Entra ID work accounts). Without that check, anyone who can set their own email at the IdP could adopt another person's Peek account.

#### Builder (`scripts/build_org_bundle.py`, Peek argument group)

New flags:

- `--peek-auto-provision` / `--no-peek-auto-provision`: `argparse.BooleanOptionalAction`, `default=None`. Help: `Create a Peek account for each person at sign-in and connect it for them (organization mode; needs --peek-base-url and a Peek provisioning key in the daemon's environment).`
- `--peek-provisioning-key-env NAME`. Help: `The environment variable the daemon reads the Peek provisioning key from (default PRIVACYFENCE_PEEK_PROVISIONING_KEY).`
- `--peek-trust-unverified-email`: `store_true`. Help: `Provision people whose ID token does not say email_verified (only for identity providers that issue only verified, organization-owned addresses, such as Microsoft Entra ID).`

Rules:

- **Merging the section.** The Peek block at `:714-717` updates `bundle.setdefault("peek", {})` instead of replacing it. With `--peek-base-url` it sets `base_url` and pops `enabled`; with `--enable-peek` it sets `enabled`. A `--merge --peek-base-url NEW` therefore keeps `auto_provision`.
- **Where the checks go.** Next to the `--web-push` check (`:840-843`), after `bundle["mode"]` is known.
  - When `args.peek_auto_provision is not None`:
    - `bundle.get("mode") != "org"` → `SystemExit("--peek-auto-provision/--no-peek-auto-provision require --mode org (or --merge against an existing org-mode bundle).")`
    - no `bundle.get("peek", {}).get("base_url")` → `SystemExit("--peek-auto-provision requires --peek-base-url (or --merge against a bundle that has one).")`
    - otherwise set `bundle["peek"]["auto_provision"] = {"enabled": args.peek_auto_provision, "key_env": args.peek_provisioning_key_env or _DEFAULT_PEEK_PROVISIONING_KEY_ENV, "trust_unverified_email": args.peek_trust_unverified_email}`.
  - When the flag is absent but `--peek-provisioning-key-env` or `--peek-trust-unverified-email` is given → `SystemExit("--peek-provisioning-key-env and --peek-trust-unverified-email go with --peek-auto-provision.")`
- **`--mode local`.** It also removes `auto_provision`: `if isinstance(bundle.get("peek"), dict): bundle["peek"].pop("auto_provision", None)`.
- **The default name.** `_DEFAULT_PEEK_PROVISIONING_KEY_ENV = "PRIVACYFENCE_PEEK_PROVISIONING_KEY"` is a module constant with a comment naming `org_mode.DEFAULT_PEEK_PROVISIONING_KEY_ENV` (the builder imports nothing from `src/`, as `_peek_url` mirrors `normalize_base_url`).

### The token file

`peek_client.save_token_file(path, base_url, token, *, source="device", expires_at=0)` writes `{"base_url", "token"}`, plus `"source": "provisioned"` and `"expires_at": <int>` only when `source == "provisioned"`. Device files stay byte-identical to today.

`load_token_file` keeps returning `dict[str, str]`. It adds:

- `"source"`: `"provisioned"` when the file says so, else `"device"`;
- `"expires_at"`: the decimal string of the integer, `"0"` when absent or not an int.

Any other `source` value → `_unreadable()`.

### `peek_client` additions

`PeekClientError.__init__(message, *, status=None, detail="")`. `_send` passes `detail=_detail(response)` on every raise that follows an HTTP answer. Every existing message stays byte-identical.

```python
@dataclass(frozen=True)
class PeekProvisionedAccount:
    external_id: str
    account_id: int
    created: bool
    adopted: bool
    disabled: bool
    has_token: bool
    token_expires_at: int

@dataclass(frozen=True)
class PeekMintedToken:
    token: str = field(repr=False)
    token_id: int
    expires_at: int

class PeekProvisioningClient:
    def __init__(self, base_url: str, key: str) -> None
    def check(self) -> int                                   # GET /api/provisioning/self -> accounts
    def upsert_account(self, external_id: str, email: str, name: str) -> PeekProvisionedAccount
    def mint_token(self, external_id: str, expires_days: int = 90) -> PeekMintedToken
    def release_account(self, external_id: str) -> bool      # False: Peek had no link (404 with detail "not found")
```

- It uses `_send` (no redirects, 30 s timeout) with its own `requests.Session`. Only the one provisioning worker thread uses an instance (see below). `__repr__` is `PeekProvisioningClient(<host>)`.
- `external_id` must match `^[A-Za-z0-9._@-]{1,200}$`, else `PeekClientError(f"Not a Peek provisioning id: {external_id!r}")`, raised before any request.
- Booleans are parsed with a new `_bool(d, key)`, which is true only for JSON `true`. A 201 mint without a token → `PeekClientError(_UNEXPECTED_SHAPE)`.
- Errors are re-raised as `PeekClientError(msg, status=exc.status, detail=exc.detail)` with exactly these messages:
  - 401 → `Peek refused the provisioning key (HTTP 401). Check the key in the daemon's environment.`
  - 404 with `detail == "no detail"` (plain mux body, any call) → `This Peek server has no provisioning API (HTTP 404). Update Peek, or turn off Peek auto-provisioning.`
  - 404 with another detail: on `mint_token` → `Peek has no provisioned account for this person (HTTP 404).`; on `release_account` → return `False`.
  - 409 → `Peek refused to provision this account (HTTP 409): {detail}`
  - 403 → `Peek refused to provision this account (HTTP 403): {detail}`
  - anything else → `_send`'s own message.
- Log only host and external id: `Peek provisioning at %s: %s %s` (host, method name, external id). Never an email, name, key or token.

### `src/privacyfence/peek_provisioning.py` (new)

**Constants.** `TOKEN_DAYS = 90`, `RENEW_BEFORE_SECONDS = 30 * 86400`, `INACTIVE_AFTER_SECONDS = 90 * 86400`, `SWEEP_INTERVAL_SECONDS = 6 * 60 * 60`, `PACE_SECONDS = 0.5`, `QUEUE_MAX = 1000`, `STATE_FILE = "credentials/peek_provisioning.json"`, `TOKEN_FILE = "credentials/peek_token.json"`.

**State file.** `users/<id>/credentials/peek_provisioning.json`, written with `secure_files.atomic_write_json` (0600):

```json
{"email": "a@x.com", "display_name": "Alice", "status": "provisioned", "detail": "", "last_sign_in": 1767225600, "updated_at": 1767225600}
```

`status` is one of:

| Status | Meaning |
|---|---|
| `provisioned` | PrivacyFence holds a provisioned token. |
| `user_connected` | The person's own device sign-in is kept. Peek is not called. |
| `admin` | Peek refused because the account is a Peek admin. The person connects by hand. |
| `disabled` | The Peek account is disabled. No mint. |
| `no_email` | The IdP sent no email. |
| `unverified_email` | The IdP did not say the email is verified, and `trust_unverified_email` is off. |
| `released` | `--peek-release` ran. The email is cleared. |
| `error` | Anything else. `detail` holds the `PeekClientError` message. |

The email is kept so the sweep can renew a token without a sign-in, but only for people seen within `INACTIVE_AFTER_SECONDS`. Someone who stops signing in loses the Peek token when it expires, at most 90 days later.

**Per-principal lock.** `principal_lock(principal_id: str) -> threading.Lock` returns one lock per id from a module dict guarded by a module lock. `routes_connect.peek_finish` saves its device file inside `with peek_provisioning.principal_lock(principal.id):`, so a provisioning save and a device sign-in never interleave.

**`provision_principal(principal_id: str, email: str, display_name: str, *, client: PeekProvisioningClient, base_url: str, on_change: Callable[[str], None], now: float) -> str`**

It returns the status it wrote, and it writes the state file with the given email, display name, status and detail. It keeps `last_sign_in` as stored. The steps:

1. If `email` is empty → `no_email`.
2. Under `principal_lock(principal_id)`, read `paths.user_dir(Principal(id=principal_id)) / TOKEN_FILE` with `load_token_file`. A `PeekClientError` means there is no usable file.
3. If the file is usable, `source == "device"` and `base_url` equals the pinned `base_url` → `user_connected`. Peek is not called.
4. Call `account = client.upsert_account(principal_id, email, name)`. `name` is `display_name`, except `""` when `display_name` equals `email` or `principal_id`; it is cut to 100 characters.
5. If `account.disabled` → `disabled`. No mint.
6. Mint only when needed. When the file is usable, `source == "provisioned"`, its `base_url` matches, `account.has_token` is true and `int(expires_at) - now > RENEW_BEFORE_SECONDS`, skip to step 7. Otherwise:
   - `minted = client.mint_token(principal_id, TOKEN_DAYS)`;
   - under `principal_lock(principal_id)`, read the token file again. If it is now a usable `device` file for `base_url`, leave it → `user_connected`. Else `save_token_file(path, base_url, minted.token, source="provisioned", expires_at=minted.expires_at)`;
   - after the lock is released, `on_change(principal_id)`.
7. Return `provisioned`.

Errors:

- A `PeekClientError` with status 409 or 403 whose `detail.startswith("account is a Peek admin")` → `admin`. If the token file is `provisioned`, delete it under the lock and call `on_change`.
- Any other `PeekClientError` → `error` with its message.
- Nothing is raised.

Logging: info `Peek provisioning for %s: %s` (principal id, status); `error` logs at warning, `Peek provisioning for %s failed: %s`. Never an email, name or token.

**`PeekProvisioner`**

```python
class PeekProvisioner:
    def __init__(self, *, client: PeekProvisioningClient, config: org_mode.PeekProvisioningConfig,
                 on_change: Callable[[str], None], stop_event: threading.Event,
                 clock: Callable[[], float] = time.time) -> None
    def request(self, principal: Principal, *, email_verified: bool) -> None
    def process_next(self, timeout: float) -> bool
    def sweep_once(self) -> int
    def start(self) -> None
```

- **`request`** never blocks. Under the state lock it first records the sign-in. It writes the state file's `email`, `display_name` and `last_sign_in = clock()`, and sets `status` to `no_email` or `unverified_email` when that applies:
  - when `principal.email` is empty → `no_email`, not queued;
  - when `email_verified` is false and `config.trust_unverified_email` is false → `unverified_email`, not queued.
  - Otherwise it enqueues `principal.id`, unless the id is already in the `_queued` set (guarded by a lock).
  - A full queue (`QUEUE_MAX`) → warning `Peek provisioning queue is full; %s waits for the next sweep or sign-in` with the id.
- **`process_next(timeout)`** takes one id from the queue, waiting up to `timeout`; it returns `False` if none came. It removes the id from `_queued`, reads that person's state file for email and display name, and runs `provision_principal(..., now=clock())`. Then it calls `stop_event.wait(PACE_SECONDS)` and returns `True`. It is the only caller of `provision_principal`. Its body is wrapped in `except Exception` → `logger.warning("Peek provisioning worker error: %s", exc)`.
- **`sweep_once`** enqueues, and never calls Peek itself. The candidates are every `users/<id>/` (safe ids, as in `web/mcp_auth.py:208-220`) whose state status is `provisioned` or `error` and whose `last_sign_in` is within `INACTIVE_AFTER_SECONDS` of `clock()`. It returns how many it enqueued.
- **`start`** starts two threads, both `daemon=True`:
  - `peek-provisioning` loops `process_next(1.0)` until `stop_event` is set.
  - `peek-provisioning-sweep` first calls `client.check()` and logs info `Peek provisioning ready at %s (%d accounts managed)`, or, on `PeekClientError`, warning `Peek provisioning check failed: %s`. In both cases it then loops `sweep_once()` followed by `stop_event.wait(SWEEP_INTERVAL_SECONDS)` until set. Each pass is wrapped in `except Exception` → warning `Peek provisioning sweep failed (will retry): %s`.

  Only the worker thread uses the client: `check()` runs before the worker has work it shares the session with. If that ordering cannot be guaranteed in a test, the sweep thread uses its own `PeekProvisioningClient` for `check()`.

**Module functions:**

```python
_INSTANCE: PeekProvisioner | None = None

def start(org_config: dict[str, Any], *, on_change: Callable[[str], None], stop_event: threading.Event) -> PeekProvisioner | None
def notify_sign_in(principal: Principal, *, email_verified: bool) -> None
def email_verified_claim(claims: dict[str, Any]) -> bool   # claims.get("email_verified") in (True, "true")
def release(principal_id: str) -> None                      # used by --peek-release: deletes a provisioned token file, writes status released with email ""
```

- **`start`.**
  - `config = PeekProvisioningConfig.from_org_config(org_config)`; not enabled → `None`.
  - `key = os.environ.get(config.key_env, "").strip()`; empty → warning `Peek auto-provisioning is on, but %s is not set; people connect Peek by hand on /connect.` (the variable name), then return `None`.
  - Otherwise build the client and the provisioner, `start()` it, set `_INSTANCE` and return it.
- **`notify_sign_in`.** It does nothing when `_INSTANCE` is `None`. Otherwise it calls `_INSTANCE.request(...)` inside `try`/`except Exception` → `logger.warning("Peek provisioning could not take the sign-in of %s: %s", principal.id, exc)`. It never raises.

### Wiring

- **`daemon_main._start_org_web_server`.**
  - Call `org_mode.PeekProvisioningConfig.from_org_config(org_config)` next to the other `from_org_config` calls (`:1158-1159`), so a broken section refuses startup.
  - Right after `connector_registry = ConnectorRegistry(...)` (`:1210`), call `peek_provisioning.start(org_config, on_change=connector_registry.evict, stop_event=_shutdown_event)`.
  - Local mode never calls it.
- **`routes_org_identity.login_callback`.** After `session_id = sessions.create(principal)` and outside the `try` block, call `peek_provisioning.notify_sign_in(principal, email_verified=peek_provisioning.email_verified_claim(claims))`.
- **`OrgOAuthProvider.handle_idp_callback`.** After `org_identity.check_authz_policy(...)`, make the same call. People who only use an AI client get provisioned too.
- **`routes_connect.peek_finish`.** Wrap its `save_token_file` (`:726-730`) in `with peek_provisioning.principal_lock(principal.id):`.
- **`--peek-release PRINCIPAL_ID`.** Add the argument next to `--revoke-scoped-tokens`. Help: `Revoke the Peek token PrivacyFence minted for the account PRINCIPAL_ID, stop renewing it, and exit. The Peek account and its pages stay. Needs the provisioning key in the environment.` It is dispatched to `run_peek_release(principal_id) -> int`. All messages go to stderr except the success line:
  1. If `safe_principal_id(principal_id) != principal_id`, `principal_id == "local"`, or `data_dir()/"users"/principal_id` is not a directory → `No such account.`, return 1.
  2. `org_config = load_org_config()` and `config = PeekProvisioningConfig.from_org_config(org_config)`. A `ConfigurationError` → print it, return 1. Not enabled → `Peek auto-provisioning is not enabled in the organization config.`, return 1.
  3. If `os.environ.get(config.key_env, "").strip()` is empty → `Set {key_env} to the Peek provisioning key first.`, return 1.
  4. `PeekProvisioningClient(config.base_url, key).release_account(principal_id)`. A `PeekClientError` → print it, return 1.
  5. `peek_provisioning.release(principal_id)`.
  6. Print to stdout `Released the Peek connection of {principal_id}. The Peek account stays.` Add ` PrivacyFence's token for it is revoked.` when Peek answered `True`, or ` Peek had no provisioned token for it.` when it answered `False`. Return 0.
  - A person who signs in again is provisioned again, so release is for people who can no longer sign in. Remove them from the identity provider first. The docs say so.

### `/connect`

In `_peek_box_html`, when connected and the record's `source == "provisioned"`, add `<div class="field-help">Your organization connected your Peek account automatically.</div>` above the Reconnect form. A device sign-in through **Reconnect Peek** saves a `device` file, which provisioning then leaves alone. The provisioned token stays live in Peek until it expires; that is known and documented.

### Rejected alternatives (they become ADR text)

- **Peek pulls the user list from PrivacyFence, or PrivacyFence exposes a webhook.** Rejected: Peek must not open connections into PrivacyFence's DMZ.
- **An ordinary Peek admin API key in PrivacyFence.** Rejected: a compromised PrivacyFence host would be a full Peek admin. The scoped key never reaches an admin account.
- **The key in the signed bundle.** Rejected for the same reason as the SIEM bearer token: bundles are copied around.
- **Provision synchronously during sign-in.** Rejected: a slow or down Peek would delay or break PrivacyFence sign-in.
- **Provision every principal at startup without a sign-in.** Not possible: emails are not stored until a person signs in once after this lands.
- **Trust every IdP email.** Rejected by default: an unverified email could adopt someone else's Peek account. It is an explicit opt-in for providers that issue only verified addresses.
- **Overwrite a person's own device sign-in.** Rejected: their choice wins, and an admin account can only be connected that way.
- **Renew forever.** Rejected: someone who left without a release would keep a live token. Renewal stops 90 days after the last sign-in.
- **Disable the Peek account on release.** Rejected: that is the Peek admin's decision, and the account may predate PrivacyFence.

## ADRs

- **0239.** *PrivacyFence provisions Peek accounts with a scoped key and only ever calls Peek.* It covers: the key is kept in an environment variable named by the bundle; provisioning starts at sign-in, never blocks it, and runs on one worker; admins are never adopted; release revokes only PrivacyFence's token. It extends ADR 0151 and ADR 0152.
- **0240.** *A person's own Peek sign-in wins over provisioning.* It covers the `source` marker in `peek_token.json` and the per-principal lock shared with `/connect`.
- **0241.** *Peek provisioning needs a verified email, keeps it for renewal, and stops renewing after 90 days without a sign-in.* It covers `email_verified`, `trust_unverified_email`, the stored email in `credentials/peek_provisioning.json`, and 90-day tokens renewed 30 days ahead.

## Manual steps

Everything in the phases runs against fakes. The real end-to-end check needs a Peek release with the companion plan, and the organization's own servers. All of it is in `manual_after`, on the [step-by-step page](https://claude.ai/artifact/CWBqWPymaC4gWn2YbCb8xA):

1. **ma1:** deploy the Peek build and create a provisioning key.
2. **ma2:** put the key into the daemon's environment, rebuild the bundle with `--peek-auto-provision`, and restart.
3. **ma3:** check a new person, an existing Peek user and a Peek admin.
4. **ma4:** check independence: Peek down at sign-in, and PrivacyFence down while using Peek.
5. **ma5:** check `--peek-release`.

## Risks and open questions

- **`_send` messages.** Adding `detail` to `PeekClientError` must not change any existing message. If an existing test in `tests/unit/test_peek_client.py` needs a change, stop with `status=blocked`.
- **Peek's error strings changed when the companion plan landed.** If so, use the landed text and say so in the phase result. Admin detection uses only `detail.startswith("account is a Peek admin")`.
- **The QA fixture dispatch is refused** (the workflow is not on `main`, or the runner is offline). Stop p2 with `status=blocked` and say which. Do not mark the QA row done.
- **`email_verified`.** If a test IdP fixture in `tests/unit/web/` already sends `email_verified` in another shape, `email_verified_claim` accepts only `True` and `"true"`. Do not widen it without stopping to ask.

## Implementation manifest

```yaml
plan_slug: peek-auto-provisioning
feature_branch: feature/peek-auto-provisioning
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/CWBqWPymaC4gWn2YbCb8xA
manual_steps_source: docs/peek-auto-provisioning-plan-manual-steps.html
manual_after:
  - id: ma1-peek-key
    title: Deploy a Peek release with the provisioning API and create a provisioning key
    why: The phases test against fakes; only a real Peek server proves the wire contract.
  - id: ma2-pf-config
    title: Put the key into the PrivacyFence daemon's environment, rebuild the bundle with --peek-auto-provision, restart
    why: Exercises the env-var key, bundle validation and the startup check on a real install.
  - id: ma3-three-people
    title: Sign in as a new person, an existing Peek user and a Peek admin; check each result
    why: Verifies create, adopt and the admin refusal against the real Peek and IdP, including email_verified.
  - id: ma4-independence
    title: Stop Peek and sign in to PrivacyFence; stop PrivacyFence and use Peek
    why: Proves neither product needs the other, which CI cannot show across two servers.
  - id: ma5-release
    title: Run privacyfence-app --peek-release for the test person
    why: Verifies the offboarding call against the real Peek.
verify_after_merge:
  - python3 -m pytest tests/unit/test_org_mode.py tests/unit/test_build_org_bundle.py tests/unit/test_peek_client.py tests/unit/test_docs_configuration_reference.py tests/unit/test_systemic_gate_invariants.py -q
  - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/web/test_routes_connect.py tests/unit/web/test_routes_org_identity.py tests/unit/web/test_oauth_provider.py -q
final_checks:
  - docs/peek-auto-provisioning-plan.md and docs/peek-auto-provisioning-plan-manual-steps.html are deleted and nothing links to them
  - docs/adr/0239-*.md, docs/adr/0240-*.md and docs/adr/0241-*.md exist, are Accepted, and are rows in docs/adr/README.md
  - CHANGELOG.md has the Peek auto-provisioning entry under "## [Unreleased]" and no new version heading
  - the PR description carries the qa-record-fixture.yml (connector=peek) result for this branch
phases:
  - id: p1-config
    title: PeekProvisioningConfig and the bundle builder flags
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/org_mode.py
      - scripts/build_org_bundle.py
      - docs/configuration-reference.md
      - tests/unit/test_org_mode.py
      - tests/unit/test_build_org_bundle.py
    brief: |
      Must-read docs: CLAUDE.md, CONTRIBUTING.md, docs/coding-and-testing-guidelines.md and docs/testing-policy.md. Then read this plan's Design section "Organization config", including "Builder".
      1. src/privacyfence/org_mode.py:
         - Add `import re`.
         - Add DEFAULT_PEEK_PROVISIONING_KEY_ENV, _ENV_NAME_RE and PeekProvisioningConfig exactly as in Design, after WebPushConfig (org_mode.py:491-514), with every ConfigurationError message verbatim. Import bundle_base_url and PeekClientError from .peek_client.
         - Add the public names to __all__.
      2. scripts/build_org_bundle.py:
         - Add the three flags to the "Peek" group (:302) with the help texts in Design.
         - Change the Peek block at :714-717 to update the section in place, as Design says.
         - Add the rules next to the --web-push check (:840-843), with the SystemExit messages verbatim.
         - Add the --mode local pop at :765-773.
         - Add the module constant.
      3. docs/configuration-reference.md: after the --peek-base-url row (:283), add one row for `--peek-auto-provision` / `--no-peek-auto-provision` (bundle key `peek.auto_provision.enabled`), one for `--peek-provisioning-key-env NAME` (`peek.auto_provision.key_env`) and one for `--peek-trust-unverified-email` (`peek.auto_provision.trust_unverified_email`), in that table's columns.
      4. Tests.
         - tests/unit/test_org_mode.py: a class TestPeekProvisioningConfigFromOrgConfig with these cases:
           - absent → disabled
           - enabled with base_url and mode org → enabled, the default key_env and the normalised base_url
           - a custom key_env
           - trust_unverified_email true
           - non-bool enabled
           - non-bool trust_unverified_email
           - bad key_env ("lower", "1ABC", "A-B")
           - enabled in local mode
           - enabled without base_url
           - enabled with base_url "http://peek.example.com" → the "is not usable" message
         - tests/unit/test_build_org_bundle.py: extend TestPeekFlags (:991) in TestWebPushFlag's style (:790) with these cases:
           - writes the section with the default env
           - writes a custom env
           - writes trust_unverified_email
           - --no-peek-auto-provision writes enabled false
           - refuses without --mode org
           - refuses without --peek-base-url
           - refuses --peek-provisioning-key-env alone
           - refuses --peek-trust-unverified-email alone
           - --mode local strips it
           - --merge with a new --peek-base-url keeps auto_provision
           - a fresh `--mode org --peek-base-url … --peek-auto-provision` build succeeds
      5. Run ruff check . and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_org_mode.py -q --cov=privacyfence.org_mode --cov-branch --cov-report=term-missing passes and reports 100% for org_mode.py
      - python3 -m pytest tests/unit/test_build_org_bundle.py -k "Peek" -q passes
      - python3 -m pytest tests/unit/test_docs_configuration_reference.py -q passes
      - ruff check . passes

  - id: p2-client
    title: Error detail, token file source marker and PeekProvisioningClient
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/peek_client.py
      - tests/unit/test_peek_client.py
      - tests/fixtures/**
    brief: |
      Read this plan's Design sections "Peek's provisioning API", "The token file" and "peek_client additions", and "Risks".
      1. Add detail to PeekClientError and pass it from every _send raise that follows an HTTP answer. Existing messages stay byte-identical, and the existing tests in tests/unit/test_peek_client.py must pass unchanged. If PeekClientError already has detail (the peek-api-alignment plan adds it the same way), keep it and skip this step; if that plan landed first, the renamed-slug branch is gone and tests/unit/test_peek_client.py reflects it.
      2. Change save_token_file and load_token_file as Design says. save_token_file stays the one write site (TOKEN_WRITE_SITES already lists it).
      3. Add _bool, PeekProvisionedAccount, PeekMintedToken and PeekProvisioningClient with the signatures, paths, bodies, error messages and log line in Design.
      4. Tests in tests/unit/test_peek_client.py, mocking requests.Session.request the way the file already does:
         - a device file round-trips without the new keys (the JSON has exactly base_url and token)
         - a provisioned file round-trips source and expires_at
         - an unknown source is unreadable
         - PeekClientError.detail is set for 403, 404, 409 and 500
         - upsert sends PUT with the JSON body and the Bearer key
         - mint parses token, token_id and expires_at
         - mint without a token → _UNEXPECTED_SHAPE
         - release → True on 200, False on 404 {"error":"not found"}
         - release on a plain-text 404 → the "no provisioning API" error
         - each mapped message: 401; plain 404 on check and upsert; JSON 404 on mint; 409 with detail; 403 with detail
         - an invalid external id is refused before any request
         - repr(PeekProvisioningClient(...)) contains the host and not the key
         - allow_redirects=False is passed
      5. Run ruff check . and the acceptance commands.
      6. QA fixture (dod_conditional for src/privacyfence/*_client.py): push this phase's branch, then dispatch qa-record-fixture.yml on main with inputs connector=peek and branch=<this phase's branch>.
         - Wait for it, then pull its commit.
         - Review the fixture diff as docs/connector-qa.md ("Reviewing recorded fixtures") says: no real account identifier, URL, token or private content.
         - Put the run URL and the --check result into the phase result.
         - If the dispatch is refused, see Risks.
    acceptance:
      - python3 -m pytest tests/unit/test_peek_client.py -q --cov=privacyfence.peek_client --cov-branch --cov-report=term-missing passes and reports 100% for peek_client.py
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py -q passes
      - grep -n "class PeekProvisioningClient" src/privacyfence/peek_client.py matches
      - the qa-record-fixture.yml run for connector=peek on this branch concludes success
      - ruff check . passes

  - id: p3-provisioner
    title: peek_provisioning module (per-person flow, single worker, sweep)
    depends_on: [p1-config, p2-client]
    complexity: M
    touches:
      - src/privacyfence/peek_provisioning.py
      - tests/unit/test_peek_provisioning.py
      - tests/conftest.py
    brief: |
      Read this plan's Design section "src/privacyfence/peek_provisioning.py" in full. Pattern for a guarded loop: daemon_main._run_personal_data_sweep (daemon_main.py:311-322).
      1. Create src/privacyfence/peek_provisioning.py with exactly the constants, state file, principal_lock, provision_principal (steps 1-7 and its errors), PeekProvisioner (request, process_next, sweep_once, start), _INSTANCE, start, notify_sign_in, email_verified_claim and release that Design describes.
         - Write every file with secure_files.atomic_write_json.
         - Every thread is daemon=True and exits once stop_event is set.
         - provision_principal is called only from process_next.
      2. tests/conftest.py _reset() (:90-145): add `peek_provisioning._INSTANCE = None` and clear the principal-lock dict. Add the module to the import list at :27-46.
      3. Tests in tests/unit/test_peek_provisioning.py.
         Setup:
         - tmp_path as the data dir: monkeypatch paths.data_dir as tests/unit/test_principal_isolation.py does.
         - A FakeProvisioningClient class records calls and can be set to raise PeekClientError(..., status=..., detail=...). No network.
         Cases:
         - test_no_email_writes_no_email
         - test_device_file_for_same_server_is_kept_and_peek_not_called
         - test_device_file_for_other_server_is_replaced
         - test_new_person_is_upserted_minted_saved_and_evicted (source provisioned and expires_at in the file; on_change called once)
         - test_name_is_empty_when_display_name_is_email_or_id
         - test_name_is_cut_to_100_characters
         - test_fresh_provisioned_token_is_not_reminted
         - test_token_within_30_days_is_reminted
         - test_has_token_false_is_reminted
         - test_disabled_account_is_not_minted
         - test_admin_409_writes_admin_and_removes_provisioned_file
         - test_admin_403_on_promoted_account_writes_admin
         - test_other_error_writes_error_with_message
         - test_device_sign_in_during_mint_wins (the fake's mint_token writes a device file before returning; the device file stays and the status is user_connected)
         - test_log_lines_carry_no_email_or_token (caplog)
         - test_request_unverified_email_is_not_queued
         - test_request_unverified_email_is_queued_when_trusted
         - test_request_records_last_sign_in
         - test_request_is_deduplicated_while_queued
         - test_full_queue_drops_with_warning
         - test_process_next_returns_false_on_empty_queue
         - test_process_next_paces_with_stop_event_wait
         - test_process_next_logs_unexpected_errors
         - test_sweep_enqueues_recent_provisioned_and_error_only (not admin, disabled, user_connected, no_email, unverified_email or released, and not those older than 90 days)
         - test_start_returns_none_when_disabled
         - test_start_returns_none_and_warns_without_key (exact message)
         - test_start_logs_ready_and_check_failed (both messages)
         - test_notify_sign_in_is_noop_without_instance
         - test_notify_sign_in_never_raises
         - test_email_verified_claim (True, "true", False, "false", absent, 1)
         - test_release_removes_provisioned_file_keeps_device_file_and_clears_email
         Drive threads deterministically: call process_next and sweep_once directly, and for start() set stop_event and join. No sleep longer than 0.1 s.
      4. Run ruff check ., python3 -m mypy src/privacyfence and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_peek_provisioning.py -q --cov=privacyfence.peek_provisioning --cov-branch --cov-report=term-missing passes and reports 100% for peek_provisioning.py
      - grep -n "peek_provisioning._INSTANCE = None" tests/conftest.py matches
      - python3 -m mypy src/privacyfence passes

  - id: p4-wiring
    title: Start the provisioner, hook both sign-in callbacks, add --peek-release
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
      Read this plan's Design section "Wiring".
      1. daemon_main._start_org_web_server (daemon_main.py:1109-1265): add the validation and the start call exactly as Design says.
      2. web/routes_org_identity.py login_callback: add the notify_sign_in call after sessions.create (:227), outside the try block at :213-224.
      3. web/oauth_provider.py handle_idp_callback: add the notify_sign_in call after check_authz_policy (:476).
      4. daemon_main: add --peek-release next to --revoke-scoped-tokens (:2405), dispatch it next to :2497, and implement run_peek_release exactly as Design steps 1-6 say, with the messages verbatim and on the streams given. Put it after run_revoke_scoped_tokens under a "# --peek-release" banner comment in the same style.
      5. docs/configuration-reference.md daemon option table (:357-368): add a row after --revoke-scoped-tokens:
         | `--peek-release PRINCIPAL_ID` | Revoke the Peek token PrivacyFence minted for that account, stop renewing it, and exit. The Peek account stays. Needs the Peek provisioning key in the environment; run it as the account the daemon runs as. |
      6. Tests.
         - tests/unit/test_daemon_main.py:
           - test_org_server_starts_peek_provisioner_with_registry_evict (monkeypatch peek_provisioning.start; assert its arguments)
           - test_org_server_refuses_broken_peek_auto_provision
           - class TestPeekRelease, against a fake PeekProvisioningClient:
             - test_unknown_account
             - test_local_is_refused
             - test_configuration_error_is_printed
             - test_not_enabled
             - test_missing_key
             - test_success_revoked_message
             - test_no_link_message
             - test_client_error_exits_1
             - test_release_called (peek_provisioning.release called with the id)
         - tests/unit/web/test_routes_org_identity.py:
           - test_login_callback_calls_notify_sign_in_with_email_verified
           - test_failed_login_callback_does_not_call_notify_sign_in
         - tests/unit/web/test_oauth_provider.py:
           - test_handle_idp_callback_calls_notify_sign_in
           - test_authorization_denied_does_not_call_notify_sign_in
      7. Run ruff check ., python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py, and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py -k "peek_provisioner or PeekRelease or peek_auto_provision" -q passes
      - python3 -m pytest tests/unit/web/test_routes_org_identity.py -k notify_sign_in -q passes
      - python3 -m pytest tests/unit/web/test_oauth_provider.py -k notify_sign_in -q passes
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
      - docs/peek-setup.md
      - docs/org-mode-setup-guide.md
      - docs/org-mode-docker.md
      - docs/security-and-compliance.md
    brief: |
      Read this plan's Design sections "/connect", "Wiring" (the routes_connect.peek_finish bullet), "Organization config" and "--peek-release".
      1. web/routes_connect.py:
         - In peek_finish, wrap the save_token_file call (:726-730) in `with peek_provisioning.principal_lock(principal.id):`.
         - In _peek_box_html (:960-997), use the record already loaded at :966 to add the exact field-help line from Design when connected and record["source"] == "provisioned".
      2. tests/unit/web/test_routes_connect.py:
         - TestPeekBox (:725): a provisioned file shows the line; a device file does not.
         - TestPeekFlow (:773): finish takes the principal lock (monkeypatch principal_lock with a recording lock).
      3. docs/peek-setup.md: add "## Automatic accounts (organization mode)" after "## Users connect". Cover:
         - what it does, and that Peek never connects to PrivacyFence
         - the Peek admin running `peek provisioner create --name PrivacyFence`
         - storing the key in the daemon's environment as PRIVACYFENCE_PEEK_PROVISIONING_KEY (or the --peek-provisioning-key-env name): the systemd EnvironmentFile and the Docker privacyfence.env
         - --peek-auto-provision
         - the verified-email rule and --peek-trust-unverified-email, and when it is safe
         - that a person's own sign-in wins
         - that Peek admins are not adopted and connect by hand
         - 90-day tokens renewed while the person signs in at least every 90 days
         - --peek-release and "remove them from the identity provider first"
         - what each product does when the other is down
         Add to "## Troubleshooting" every new message in Design (startup warnings, the ready and check-failed lines, the PeekProvisioningClient errors, the --peek-release messages, and the queue-full warning), each with its fix. Add the new bundle flags to "## Build and distribute the bundle".
      4. docs/org-mode-setup-guide.md:
         - The connector table's Peek row and note (:240, :255-256): add "or automatic accounts with --peek-auto-provision ([Peek setup](peek-setup.md#automatic-accounts-organization-mode))".
         - The bundle option table (:341): add the three new options.
         - The systemd unit comment (:656-657): make it "For --audit-forwarding-http-bearer-token-env and the Peek provisioning key; …".
         - The users/<principal> table (:1061-1067): add `users/<principal>/credentials/peek_provisioning.json`: "Peek auto-provisioning state and the person's email, for token renewal."
      5. docs/org-mode-docker.md:
         - Next to :169, add the same note for the Peek provisioning key in privacyfence.env.
         - Next to :206, add `docker compose exec privacyfence privacyfence-app --peek-release PRINCIPAL_ID`.
      6. docs/security-and-compliance.md, the offboarding text at :262-263: add running --peek-release when Peek auto-provisioning is on.
      7. Run ruff check . and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_connect.py -k Peek -q passes
      - grep -n "## Automatic accounts (organization mode)" docs/peek-setup.md matches
      - grep -n "peek_provisioning.json" docs/org-mode-setup-guide.md matches
      - grep -n "peek-release" docs/security-and-compliance.md docs/org-mode-docker.md matches in both files

  - id: p6-retire
    title: ADRs 0239-0241, changelog, delete the plan
    depends_on: [p4-wiring, p5-connect-and-docs]
    complexity: S
    touches:
      - docs/adr/0239-*.md
      - docs/adr/0240-*.md
      - docs/adr/0241-*.md
      - docs/adr/README.md
      - CHANGELOG.md
      - docs/peek-auto-provisioning-plan.md
      - docs/peek-auto-provisioning-plan-manual-steps.html
    brief: |
      Read docs/adr/README.md (template, rules, one decision per ADR). Take the next free numbers: 0239, 0240 and 0241 unless main has used them, in which case take the next free ones and update every reference.
      1. Write the three ADRs from this plan's ADRs section, each Status Accepted.
         - docs/adr/0239-privacyfence-provisions-peek-accounts-with-a-scoped-key-and-only-calls-peek.md
         - docs/adr/0240-a-persons-own-peek-sign-in-wins-over-provisioning.md
         - docs/adr/0241-peek-provisioning-needs-a-verified-email-and-stops-renewing-after-90-days.md
         Each has Context (ADR 0151, ADR 0152 and the separate DMZs), Decision, Consequences, and the matching rejected alternatives from Design "Rejected alternatives". Link source files (src/privacyfence/peek_provisioning.py, peek_client.py, org_mode.py) and https://github.com/andras-tkcs/peek/blob/main/docs/src/content/docs/auth-access.md, never this plan.
      2. Add the three rows to docs/adr/README.md in the existing format.
      3. CHANGELOG.md: under "## [Unreleased]" add "### Added" and one entry in the file's bold-lead style:
         "- **Peek accounts for everyone, in organization mode.** With `--peek-auto-provision` and a Peek provisioning key, each person who signs in gets a Peek account (or keeps their existing one) connected for them; their own Peek sign-in still wins, and `privacyfence-app --peek-release` revokes PrivacyFence's token when someone leaves."
         Never add a version heading.
      4. git rm docs/peek-auto-provisioning-plan.md docs/peek-auto-provisioning-plan-manual-steps.html, and grep that nothing links to them.
      5. Run ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - test ! -e docs/peek-auto-provisioning-plan.md && test ! -e docs/peek-auto-provisioning-plan-manual-steps.html
      - grep -rn "peek-auto-provisioning-plan" docs src README.md CHANGELOG.md prints nothing
      - ls docs/adr/0239-*.md docs/adr/0240-*.md docs/adr/0241-*.md lists three files
      - grep -n "0241" docs/adr/README.md matches
      - python3 -m pytest tests/unit -q passes
```
