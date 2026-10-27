# Peek auto-provisioning in organization mode — plan

## Goal

Today, organization mode offers Peek on `/connect`, and each person connects it by hand with Peek's device sign-in. With this change an organization can turn on **Peek auto-provisioning** in its bundle. Each person who signs in to PrivacyFence then gets a Peek account on the organization's Peek server, or keeps their existing one, and that account is registered as their Peek connection with no clicks.

- PrivacyFence uses a scoped **provisioning key** that a Peek admin creates. All calls go from PrivacyFence to Peek; Peek never connects to PrivacyFence, because the two run in separate DMZs.
- Each product keeps working without the other:
  - PrivacyFence sign-in never waits on Peek, and a Peek outage only delays provisioning.
  - Peek keeps its own users, including people who are not PrivacyFence users, and its own sign-in.
- A person's own device sign-in always wins over provisioning.
- An administrator can release a departing person's Peek connection with one command.

The Peek side is the companion plan `docs/plans/pf-provisioning-plan.md` on `plan/pf-provisioning` in `andras-tkcs/peek`. This plan builds against its wire contract, restated in "Peek's provisioning API" below, and against fakes in tests. No issue tracks it.

## Current state

- **Org config.** `org_config.json` is loaded by `load_org_config` (`src/privacyfence/daemon_main.py:451-529`).
  - The Peek section has only `peek.base_url` (written by `--peek-base-url`) and `peek.enabled` (`--enable-peek`, local only). See `scripts/build_org_bundle.py:302-307`, `:314-318` and `:714-717`.
  - `peek_client.bundle_base_url` (`src/privacyfence/peek_client.py:86-97`) normalises the address.
  - The strict flag-validation pattern to copy is `WebPushConfig.from_org_config` (`src/privacyfence/org_mode.py:491-514`). The "secret in an environment variable named by the bundle" precedent is `AuditForwardingConfig.http_bearer_token_env` (`org_mode.py:382-389`).
  - The builder's `--web-push` (`build_org_bundle.py:533` and `:840-843`) is the pattern for an org-only `BooleanOptionalAction`.
  - `--mode local` strips org-only sections at `build_org_bundle.py:765-773`.
  - `tests/unit/test_docs_configuration_reference.py` requires every builder option in `docs/configuration-reference.md`; the Peek rows are at `:283` and `:285`.
- **Principals.** `Principal(id, email, display_name, is_admin)` is at `src/privacyfence/principal.py:46-65`. It is created only at OIDC sign-in by `org_identity.principal_from_claims` (`src/privacyfence/org_identity.py:279-299`). There are two sign-in callbacks:
  - browser `login_callback` (`src/privacyfence/web/routes_org_identity.py:200-238`; the principal is final after `check_authz_policy` at `:223`, and the session is created at `:227`);
  - MCP-client `OrgOAuthProvider.handle_idp_callback` (`src/privacyfence/web/oauth_provider.py:441-488`; `check_authz_policy` at `:476`).
- **Per-person storage.** Per-person data lives under `users/<principal id>/` (`paths.user_dir`, `src/privacyfence/paths.py:211-234`). Principal directories are enumerated by `web/mcp_auth.py:208-220` (`scoped_token_principals`). Email addresses are not persisted per person today.
- **Peek credential.**
  - `credentials/peek_token.json` holds `{"base_url", "token"}` (`daemon_main.py:213`). It is written by `peek_client.save_token_file` (`peek_client.py:261-262`), a registered site in `tests/unit/test_systemic_gate_invariants.py:192`. It is read by `load_token_file` (`:265-284`) and checked by `resolve_server` (`:291-297`).
  - The connector is built from it in `build_connectors` (`daemon_main.py:1695-1709`).
  - Org `/connect` device sign-in is at `web/routes_connect.py:676-743`. It saves the file, then calls `connector_registry.evict(principal.id)` (`:730`). The box is rendered by `_peek_box_html` (`:960-997`).
- **Background work.** `_run_personal_data_sweep` (`daemon_main.py:308-322`) is the loop pattern: work, `except Exception` → warning, `_shutdown_event.wait(interval)`. It is started at `:2192-2194`. The org server is assembled in `_start_org_web_server` (`daemon_main.py:1110-1265`), where `connector_registry` is created at `:1209`.
- **Offboarding precedent.** `--revoke-scoped-tokens PRINCIPAL_ID` is handled by `run_revoke_scoped_tokens` (`daemon_main.py:2323-2346`), with the argument at `:2405` and dispatch at `:2497`.
- **Module state resets.** These live in `tests/conftest.py` `_reset()` (`:90-145`); new module singletons get a line there.
- **Peek server.** Peek has no provisioning API until the companion plan lands. Its plain mux answers unknown routes with HTTP 404 and a non-JSON body.

## Design

### Peek's provisioning API (the wire contract, from the companion plan)

All requests carry `Authorization: Bearer <provisioning key>`. The key starts with `peekprov_`. `{external_id}` is the PrivacyFence principal id. It already matches `^[A-Za-z0-9._@-]{1,200}$` because of `safe_principal_id`.

| Call | Body | Success | Errors PrivacyFence handles |
|---|---|---|---|
| `GET /api/provisioning/self` | none | 200 `{"name", "accounts"}` | 401 (bad key), 404 (no provisioning API) |
| `PUT /api/provisioning/accounts/{external_id}` | `{"email", "name"}` | 200 `{"external_id", "account_id", "email", "name", "created", "adopted", "disabled", "has_token", "token_expires_at"}` | 400, 401, 403 `account email domain is not allowed`, 404 (no API), 409 `account is a Peek admin; connect it by hand`, 409 `account is linked to another external id or email` |
| `POST /api/provisioning/accounts/{external_id}/token` | `{"expires_days": 90}` | 201 `{"token", "token_id", "expires_at"}` (unix seconds) | 403 `account disabled`, 403 domain, 404, 409 admin |
| `DELETE /api/provisioning/accounts/{external_id}` | none | 200 `{"released": true}` | 404 (no link: treated as already released) |

The minted token is an ordinary Peek bearer token for that person's account. Every existing `PeekClient` call works with it.

### Organization config

```json
"peek": {
  "base_url": "https://peek.example.com",
  "auto_provision": {"enabled": true, "key_env": "PRIVACYFENCE_PEEK_PROVISIONING_KEY"}
}
```

Add to `src/privacyfence/org_mode.py`, after `WebPushConfig`, and to `__all__`:

```python
DEFAULT_PEEK_PROVISIONING_KEY_ENV = "PRIVACYFENCE_PEEK_PROVISIONING_KEY"

@dataclass(frozen=True)
class PeekProvisioningConfig:
    enabled: bool = False
    key_env: str = DEFAULT_PEEK_PROVISIONING_KEY_ENV

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "PeekProvisioningConfig": ...
```

`from_org_config` behaviour:

- **Sections.** Read `peek` (a dict, else `{}`), then `auto_provision` (a dict, else `{}`). Absent → `PeekProvisioningConfig()`.
- **Validation errors.** Each raises `ConfigurationError` with exactly this message:
  - `enabled` present and not a bool: `org_config.json's "peek"."auto_provision"."enabled" must be true or false, got {value!r}`
  - `key_env` present and not matching `^[A-Z_][A-Z0-9_]{0,127}$`: `org_config.json's "peek"."auto_provision"."key_env" must be an environment variable name, got {value!r}`
  - enabled while `resolve_mode(org_config) != "org"`: `org_config.json's "peek"."auto_provision" needs "mode": "org"`
  - enabled while `peek.base_url` is empty: `org_config.json's "peek"."auto_provision" needs "peek"."base_url"`

The key itself is never in the bundle (the same reasoning as `http_bearer_token_env`). The daemon reads `os.environ[key_env]` when it starts the provisioner and when `--peek-release` runs.

Builder (`scripts/build_org_bundle.py`, Peek argument group):

- `--peek-auto-provision` / `--no-peek-auto-provision`: `action=argparse.BooleanOptionalAction, default=None`. Help text: `Create a Peek account for each person at sign-in and connect it for them (organization mode; needs --peek-base-url and a Peek provisioning key in the daemon's environment).`
- `--peek-provisioning-key-env NAME`. Help text: `The environment variable the daemon reads the Peek provisioning key from (default PRIVACYFENCE_PEEK_PROVISIONING_KEY).`
- **When `args.peek_auto_provision is not None`:**
  - If `bundle.get("mode") != "org"` → `SystemExit("--peek-auto-provision/--no-peek-auto-provision require --mode org (or --merge against an existing org-mode bundle).")`.
  - If the bundle has no `peek.base_url` after the Peek block → `SystemExit("--peek-auto-provision requires --peek-base-url (or --merge against a bundle that has one).")`.
  - Otherwise set `bundle["peek"]["auto_provision"] = {"enabled": args.peek_auto_provision, "key_env": args.peek_provisioning_key_env or DEFAULT}`. When the flag is absent but `--peek-provisioning-key-env` is given → `SystemExit("--peek-provisioning-key-env goes with --peek-auto-provision.")`.
- **`--mode local`** also removes `peek.auto_provision`: `bundle.get("peek", {}).pop("auto_provision", None)`.

### The token file

`peek_client.save_token_file(path, base_url, token, *, source="device", expires_at=0)` writes `{"base_url", "token"}`, plus `"source": "provisioned"` and `"expires_at": <int>` only when `source == "provisioned"`. Device files stay byte-identical to today.

`load_token_file` returns `dict[str, str]` with the existing keys plus:

- `"source"`: `"provisioned"` when the file says so, else `"device"`;
- `"expires_at"`: the decimal string of the integer, `"0"` when absent or not an int.

`source` values other than `"device"` and `"provisioned"` → `_unreadable()`. Callers that read only `base_url` and `token` are unchanged.

### `peek_client` additions

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
    def check(self) -> int                                   # GET /api/provisioning/self → accounts
    def upsert_account(self, external_id: str, email: str, name: str) -> PeekProvisionedAccount
    def mint_token(self, external_id: str, expires_days: int = 90) -> PeekMintedToken
    def release_account(self, external_id: str) -> bool      # False when Peek answered 404 (no link)
```

- **Request path.** It uses `_send` like `PeekClient._request`, with redirects refused and a 30 s timeout. `__repr__` shows the host only, never the key.
- **External id.** It must match `^[A-Za-z0-9._@-]{1,200}$`, else `PeekClientError("Not a Peek provisioning id: …")`.
- **Error mapping.** Catch the `PeekClientError` from `_send` and re-raise `PeekClientError(msg, status=…)` with these exact messages (`{d}` is `_detail(response)`, so `_send` must keep the detail available; see the phase brief):
  - 401 → `Peek refused the provisioning key (HTTP 401). Check the key in the daemon's environment.`
  - 404 on `check` or `upsert_account` → `This Peek server has no provisioning API (HTTP 404). Update Peek, or turn off Peek auto-provisioning.`
  - 404 on `mint_token` → `Peek has no provisioned account for this person (HTTP 404).`
  - 409 → `Peek refused to provision this account (HTTP 409): {d}`
  - 403 → `Peek refused to provision this account (HTTP 403): {d}`
  - anything else → the message `_send` produced.

### `src/privacyfence/peek_provisioning.py` (new)

**Constants:** `TOKEN_DAYS = 90`, `RENEW_BEFORE_SECONDS = 30 * 86400`, `SWEEP_INTERVAL_SECONDS = 6 * 60 * 60`, `SWEEP_PACE_SECONDS = 1.0`, `STATE_FILE = "credentials/peek_provisioning.json"`.

**State file.** It lives at `users/<id>/credentials/peek_provisioning.json` and is written with `secure_files.atomic_write_json` (0600):

```json
{"email": "a@x.com", "display_name": "Alice", "status": "provisioned", "detail": "", "updated_at": 1767225600}
```

`status` is one of:

- `provisioned`: PrivacyFence holds a provisioned token;
- `user_connected`: the person's own device sign-in is kept; no Peek call;
- `admin`: Peek refused because the account is a Peek admin; that person connects by hand;
- `no_email`: the IdP sent no email;
- `error`: anything else; `detail` holds the `PeekClientError` message.

The email is kept so the sweep can renew without a sign-in. It sits beside the person's tokens, under the same permissions.

**`provision_principal(principal_id, email, display_name, *, client, base_url, now) -> str`.** Returns the status it wrote. It runs these steps in order:

1. Empty `email` → write `no_email`, return.
2. Read the token file `paths.user_dir(Principal(id=principal_id)) / "credentials/peek_token.json"` with `load_token_file`. A `PeekClientError` means "no usable file".
3. If the file is usable, `source == "device"` and `base_url` equals the pinned `base_url` → write `user_connected`, return. Peek is not called.
4. Call `client.upsert_account(principal_id, email, display_name)`.
5. Mint only when needed. Skip the mint when the file is usable, `source == "provisioned"`, its `base_url` equals the pinned one, `account.has_token` is true, and `int(expires_at) - now > RENEW_BEFORE_SECONDS`. Otherwise:
   - `client.mint_token(principal_id, TOKEN_DAYS)`;
   - `save_token_file(path, base_url, minted.token, source="provisioned", expires_at=minted.expires_at)`;
   - then call the `on_change(principal_id)` callback.
6. Write `provisioned`, return.

A `PeekClientError` with status 409 whose message contains `Peek admin` → write `admin`. Any other `PeekClientError` → write `error` with its message. Neither is raised.

Logs go at info level: `Peek provisioning for %s: %s` (principal id and status). An error logs at warning with the message. No email, name or token ever appears in a log line.

**`PeekProvisioner`.** It owns one worker thread and one sweep thread.

```python
class PeekProvisioner:
    def __init__(self, *, client: PeekProvisioningClient, base_url: str,
                 on_change: Callable[[str], None], stop_event: threading.Event,
                 clock: Callable[[], float] = time.time) -> None
    def request(self, principal: Principal) -> None   # non-blocking; enqueues (id, email, display_name); drops a duplicate already queued
    def sweep_once(self) -> int                       # for every users/<id>/ with a state file whose status is provisioned or error: provision_principal with the stored email; waits SWEEP_PACE_SECONDS between principals via stop_event.wait; returns how many it ran
    def start(self) -> None                           # starts thread "peek-provisioning" (drains the queue) and thread "peek-provisioning-sweep" (logs client.check() once, then sweep_once every SWEEP_INTERVAL_SECONDS until stop_event)
```

- **Queue.** The worker writes the state file's `email` and `display_name` from the request before calling `provision_principal`, so a sign-in refreshes them. The queue is a `queue.Queue` with `maxsize=1000`; when it is full, `request` logs a warning and drops the item, and the sweep or the next sign-in retries.
- **Errors.** Every loop body is wrapped in `except Exception` → `logger.warning(..., exc)`, as `_run_personal_data_sweep` does.

**Module singleton:**

```python
_INSTANCE: PeekProvisioner | None = None

def start(org_config: dict[str, Any], *, on_change: Callable[[str], None], stop_event: threading.Event) -> PeekProvisioner | None
def notify_sign_in(principal: Principal) -> None     # no-op when _INSTANCE is None
```

`start`:

- `config = org_mode.PeekProvisioningConfig.from_org_config(org_config)`. When it is not enabled, return `None`.
- Read `key = os.environ.get(config.key_env, "").strip()`. When it is empty, log a warning with exactly `Peek auto-provisioning is on, but %s is not set; people connect Peek by hand on /connect.` (the variable name), and return `None`.
- `base_url = peek_client.bundle_base_url(org_config["peek"], org_mode=True)`.
- Build the client and the provisioner, call `start()`, set `_INSTANCE`, and return it.

### Wiring

- **`daemon_main._start_org_web_server`.** Right after `connector_registry = ConnectorRegistry(...)` (`:1209`), call `peek_provisioning.start(org_config, on_change=connector_registry.evict, stop_event=_shutdown_event)`. Also call `org_mode.PeekProvisioningConfig.from_org_config(org_config)` once before that, with the other `from_org_config` calls (`:1158-1159`), so a broken section refuses startup.
- **`routes_org_identity.login_callback`.** After `session_id = sessions.create(principal)`, call `peek_provisioning.notify_sign_in(principal)`.
- **`OrgOAuthProvider.handle_idp_callback`.** After `org_identity.check_authz_policy(...)`, call `peek_provisioning.notify_sign_in(principal)`. People who only use an AI client get provisioned too.
- **`--peek-release PRINCIPAL_ID`.** New argument next to `--revoke-scoped-tokens`. Help text: `Revoke the Peek token PrivacyFence minted for the account PRINCIPAL_ID and forget it, and exit. The Peek account and its pages stay. Needs the provisioning key in the environment.` It is dispatched to `run_peek_release(principal_id) -> int`:
  - Validate the id exactly as `run_revoke_scoped_tokens` does, printing `No such account.` and returning 1.
  - `org_config = load_org_config()`, then `config = PeekProvisioningConfig.from_org_config(org_config)`.
    - Not enabled → print `Peek auto-provisioning is not enabled in the organization config.` to stderr, return 1.
    - Empty key → `Set {key_env} to the Peek provisioning key first.`, return 1.
  - `PeekProvisioningClient(bundle_base_url(...), key).release_account(principal_id)`. A `PeekClientError` → print it to stderr, return 1.
  - Delete `credentials/peek_token.json` if `load_token_file` reports `source == "provisioned"`, and delete the state file.
  - Print `Released the Peek connection of {principal_id}; PrivacyFence's Peek token for it is revoked. The Peek account stays.` and return 0.
  - Runs with the daemon running: the cached connector then gets 401 from Peek, which is the existing "connect again" message.

### `/connect`

In `_peek_box_html`, when connected and the record's `source == "provisioned"`, add `<div class="field-help">Your organization connected your Peek account automatically.</div>` above the button. The button keeps its `Reconnect Peek` label. A device sign-in through it saves a `device` file, which provisioning then leaves alone.

### Rejected alternatives (they become ADR text)

- **Peek pulls the user list from PrivacyFence, or PrivacyFence exposes a webhook.** Rejected: Peek must not open connections into PrivacyFence's DMZ.
- **An ordinary Peek admin API key in PrivacyFence.** Rejected: a compromised PrivacyFence host would be a full Peek admin. The scoped key never touches admin accounts.
- **The key in the signed bundle.** Rejected for the same reason as the SIEM bearer token: bundles are copied around. The bundle names an environment variable.
- **Provision synchronously during sign-in.** Rejected: a slow or down Peek would delay or break PrivacyFence sign-in.
- **Provision every principal directory at startup without a sign-in.** Not possible: emails are not stored until a person signs in once after this lands. The sweep covers everyone who has.
- **Overwrite a person's own device sign-in.** Rejected: their choice wins, and an admin account can only be connected that way.
- **Disable the Peek account on release.** Rejected: that is the Peek admin's decision, and the account may predate PrivacyFence.

## ADRs

- **0239.** *PrivacyFence provisions Peek accounts through a scoped key, calling Peek only, never called by it.* It records: scoped key in an env var named by the bundle; sign-in-triggered and swept, never blocking sign-in; admins never adopted; release revokes only PrivacyFence's token. It extends ADR 0151 (device sign-in stays) and ADR 0152 (the token stays with the pinned server).
- **0240.** *A person's own Peek sign-in wins over provisioning, and PrivacyFence keeps each provisioned person's email for renewal.* It records the `source` marker in `peek_token.json`, 90-day tokens renewed 30 days ahead, and the email kept in `credentials/peek_provisioning.json`.

## Manual steps

Everything in the phases runs against fakes. The real end-to-end check needs a Peek release that includes the companion plan, plus the organization's own servers, so it is `manual_after` only. See the step-by-step page linked in the manifest (`manual_steps_artifact`).

- **ma1:** deploy the Peek build and create the provisioning key.
- **ma2:** put the key into the PrivacyFence daemon's environment, rebuild the bundle with `--peek-auto-provision`, and restart.
- **ma3:** check that a new person, an existing Peek user, and a Peek admin each get the right result.
- **ma4:** check independence: Peek down at sign-in, PrivacyFence down while using Peek.
- **ma5:** check `--peek-release`.

## Risks and open questions

- **`_send` hides the 409/403 detail.** `_send` formats 403 itself and maps 404 to a page message. A phase that needs the raw status and detail must not parse the message text. Instead, extend `PeekClientError` with a `detail: str = ""` keyword that `_send` fills from `_detail(response)` for every non-2xx answer. If that would change an existing message, stop with `status=blocked`.
- **Peek's 409 text changes.** The admin case is recognised by `Peek admin` in the detail. If the companion plan's error strings changed when it landed, use the landed text and say so in the phase result.
- **`connector_registry.evict` from a non-event-loop thread.** It takes its own lock (`connector_registry.py:134-140`). If a test shows it must run on the web loop, stop with `status=blocked`.
- **Principal ids.** If `safe_principal_id` can produce a character outside `[A-Za-z0-9._@-]`, the Peek regexp would refuse it. Stop and report.
- **`test_docs_configuration_reference.py` is strict.** Both halves of `--peek-auto-provision/--no-peek-auto-provision` and `--peek-provisioning-key-env` must be in `docs/configuration-reference.md`.

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
    why: Exercises the env-var key, bundle validation and the startup log line on a real install.
  - id: ma3-three-people
    title: Sign in as a new person, an existing Peek user and a Peek admin; check each result
    why: Verifies create, adopt and the admin refusal against the real Peek and IdP.
  - id: ma4-independence
    title: Stop Peek and sign in to PrivacyFence; stop PrivacyFence and use Peek
    why: Proves that neither product needs the other, which CI cannot show across two servers.
  - id: ma5-release
    title: Run privacyfence-app --peek-release for the test person
    why: Verifies the offboarding call against the real Peek.
verify_after_merge:
  - python3 -m pytest tests/unit/test_org_mode.py tests/unit/test_build_org_bundle.py tests/unit/test_peek_client.py tests/unit/test_docs_configuration_reference.py -q
  - python3 -m pytest tests/unit/test_peek_provisioning.py tests/unit/test_daemon_main.py tests/unit/web/test_routes_connect.py tests/unit/web/test_routes_org_identity.py tests/unit/web/test_oauth_provider.py tests/unit/test_systemic_gate_invariants.py -q
final_checks:
  - docs/peek-auto-provisioning-plan.md and docs/peek-auto-provisioning-plan-manual-steps.html are deleted and nothing links to them
  - docs/adr/0239-*.md and docs/adr/0240-*.md exist, are Accepted, and are rows in docs/adr/README.md
  - CHANGELOG.md has the Peek auto-provisioning entry under "## [Unreleased]" and no new version heading
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
      Must-read docs: CLAUDE.md, CONTRIBUTING.md, docs/coding-and-testing-guidelines.md, docs/testing-policy.md. Then read this plan's Design section "Organization config".
      1. In src/privacyfence/org_mode.py, after WebPushConfig (org_mode.py:491-514):
         - Add DEFAULT_PEEK_PROVISIONING_KEY_ENV and PeekProvisioningConfig exactly as in Design, with the four ConfigurationError messages verbatim.
         - Use a module-level `_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")` with fullmatch.
         - Add both names to __all__.
      2. In scripts/build_org_bundle.py:
         - Add --peek-auto-provision/--no-peek-auto-provision and --peek-provisioning-key-env to the "Peek" argument group (build_org_bundle.py:302), with the help texts in Design.
         - After the Peek block at :714-717 (and after --merge has been applied, wherever --web-push's check reads bundle.get("mode")), apply the rules and SystemExit messages in Design verbatim.
         - Import nothing from src/: hard-code "PRIVACYFENCE_PEEK_PROVISIONING_KEY" as a module constant with a comment naming org_mode.DEFAULT_PEEK_PROVISIONING_KEY_ENV, as _peek_url mirrors normalize_base_url.
         - In the `--mode local` branch (:765-773), drop peek.auto_provision.
      3. In docs/configuration-reference.md, add three rows after the --peek-base-url row (:283). Use the table's own columns: option, default, bundle key `peek.auto_provision.enabled` / `peek.auto_provision.key_env`, description. Name both halves `--peek-auto-provision` and `--no-peek-auto-provision`.
      4. Tests:
         - tests/unit/test_org_mode.py: a class TestPeekProvisioningConfigFromOrgConfig with these cases:
           - absent → disabled
           - enabled with base_url and mode org → enabled, default key_env
           - custom key_env
           - non-bool enabled
           - bad key_env ("lower", "1ABC", "A-B")
           - enabled in local mode
           - enabled without base_url
         - tests/unit/test_build_org_bundle.py: extend TestPeekFlags (:991) after TestWebPushFlag's style (:790) with these cases:
           - writes the section with the default env
           - writes a custom env
           - --no-peek-auto-provision writes enabled false
           - refuses without --mode org
           - refuses without --peek-base-url
           - refuses --peek-provisioning-key-env alone
           - --mode local strips it
           - --merge keeps an existing base_url
      5. Run ruff check . and python3 -m pytest tests/unit/test_org_mode.py tests/unit/test_build_org_bundle.py tests/unit/test_docs_configuration_reference.py -q.
    acceptance:
      - python3 -m pytest tests/unit/test_org_mode.py -k PeekProvisioning -q passes
      - python3 -m pytest tests/unit/test_build_org_bundle.py -k "Peek" -q passes
      - python3 -m pytest tests/unit/test_docs_configuration_reference.py -q passes
      - grep -n "class PeekProvisioningConfig" src/privacyfence/org_mode.py matches
      - ruff check . passes

  - id: p2-client
    title: Token file source marker and PeekProvisioningClient
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/peek_client.py
      - tests/unit/test_peek_client.py
    brief: |
      Read this plan's Design sections "Peek's provisioning API", "The token file" and "peek_client additions", and Risks ("_send hides the 409/403 detail").
      1. PeekClientError: add a keyword-only `detail: str = ""` attribute. In _send, pass detail=_detail(response) on every raise that follows an HTTP answer (3xx, 401, 403, 404, other non-2xx). Every existing message stays byte-identical; the existing tests in tests/unit/test_peek_client.py must pass unchanged. If one needs a change, stop with status=blocked.
      2. save_token_file(path, base_url, token, *, source="device", expires_at=0), and load_token_file returning "source" and "expires_at", exactly as Design says. Keep save_token_file as the one write site (TOKEN_WRITE_SITES in tests/unit/test_systemic_gate_invariants.py lists it already).
      3. Add PeekProvisionedAccount, PeekMintedToken and PeekProvisioningClient with the signatures, paths, bodies and error messages in Design.
         - Parse responses with the existing _as_dict/_str/_int helpers, and a `_bool(d, key)` helper that is True only for a JSON true.
         - A missing token in a 201 mint answer → PeekClientError(_UNEXPECTED_SHAPE).
         - Log only host and counts: "Peek provisioning at %s: account %s" with the external id is allowed; never email, name, key or token.
      4. Tests in tests/unit/test_peek_client.py, mocking requests.Session.request with the file's existing helper/pattern:
         - a device file round-trips without the new keys (assert the written JSON has exactly base_url and token)
         - a provisioned file round-trips source and expires_at
         - an unknown source → unreadable
         - upsert sends PUT with the JSON body and the Bearer key
         - mint parses token, token_id, expires_at
         - release returns True on 200 and False on 404
         - each mapped error message: 401; 404 on upsert; 404 on mint; 409 (detail included); 403
         - an invalid external id is refused before any request
         - repr(PeekProvisioningClient(...)) does not contain the key
         - redirects are refused (allow_redirects=False asserted)
      5. Run ruff check . and python3 -m pytest tests/unit/test_peek_client.py tests/unit/test_systemic_gate_invariants.py -q.
    acceptance:
      - python3 -m pytest tests/unit/test_peek_client.py -q passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py -q passes
      - grep -n "class PeekProvisioningClient" src/privacyfence/peek_client.py matches
      - ruff check . passes

  - id: p3-provisioner
    title: peek_provisioning module (per-person flow, worker, sweep)
    depends_on: [p1-config, p2-client]
    complexity: M
    touches:
      - src/privacyfence/peek_provisioning.py
      - tests/unit/test_peek_provisioning.py
      - tests/conftest.py
    brief: |
      Read this plan's Design section "src/privacyfence/peek_provisioning.py" in full. Pattern for a guarded background loop: daemon_main._run_personal_data_sweep (daemon_main.py:308-322).
      1. Create src/privacyfence/peek_provisioning.py with the constants, the state file format, provision_principal (its six steps, in order), PeekProvisioner, _INSTANCE, start and notify_sign_in, exactly as Design says.
         - Write files with secure_files.atomic_write_json.
         - Find principal directories the way web/mcp_auth.py:208-220 does: data_dir()/"users", safe ids only, skipping "local".
         - Every thread is daemon=True and exits when stop_event is set.
      2. tests/conftest.py: add `peek_provisioning._INSTANCE = None` to _reset() (conftest.py:90-145), and add the module to the import list at :27-46.
      3. Tests in tests/unit/test_peek_provisioning.py:
         - Use tmp_path as the data dir (monkeypatch paths.data_dir the way tests/unit/test_paths.py or test_principal_isolation.py does).
         - Use a fake PeekProvisioningClient class (no network) that records calls and can raise PeekClientError(…, status=409/403/500).
         - Cases:
           - test_no_email_writes_no_email
           - test_device_file_for_same_server_is_kept_and_peek_not_called
           - test_device_file_for_other_server_is_replaced
           - test_new_person_is_upserted_minted_saved_and_evicted (the file has source provisioned and expires_at; on_change called once with the id)
           - test_fresh_provisioned_token_is_not_reminted
           - test_token_within_30_days_is_reminted
           - test_has_token_false_is_reminted
           - test_admin_409_writes_admin
           - test_other_error_writes_error_with_message
           - test_log_lines_carry_no_email_or_token (caplog)
           - test_request_is_deduplicated_while_queued
           - test_full_queue_drops_with_warning
           - test_sweep_runs_provisioned_and_error_only (not admin, user_connected or no_email)
           - test_sweep_paces_with_stop_event_wait
           - test_start_returns_none_when_disabled
           - test_start_returns_none_and_warns_without_key (exact message from Design)
           - test_notify_sign_in_is_noop_without_instance
         - Drive the worker deterministically: call the worker's single-item method directly, or start() with a stop_event you set, joining the threads. No sleeps longer than 0.1s.
      4. Run ruff check ., python3 -m mypy src/privacyfence and python3 -m pytest tests/unit/test_peek_provisioning.py -q --cov=privacyfence.peek_provisioning --cov-branch --cov-report=term-missing (the coverage floor is ratcheted; aim for 100% of the new module).
    acceptance:
      - python3 -m pytest tests/unit/test_peek_provisioning.py -q passes
      - python3 -m pytest tests/unit/test_peek_provisioning.py -q --cov=privacyfence.peek_provisioning --cov-branch --cov-report=term-missing reports 100% for peek_provisioning.py
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
    brief: |
      Read this plan's Design section "Wiring".
      1. daemon_main._start_org_web_server (daemon_main.py:1110-1265):
         - Validate with org_mode.PeekProvisioningConfig.from_org_config(org_config) next to the other from_org_config calls (:1158-1159).
         - After connector_registry is created (:1209), call peek_provisioning.start(org_config, on_change=connector_registry.evict, stop_event=_shutdown_event).
         - Local mode must not call it at all.
      2. web/routes_org_identity.py login_callback: after `session_id = sessions.create(principal)` (:227), call peek_provisioning.notify_sign_in(principal). It must not be inside the try/except at :213-224, so a provisioning problem can never turn into "Sign-in failed".
      3. web/oauth_provider.py handle_idp_callback: after org_identity.check_authz_policy(...) (:476), call peek_provisioning.notify_sign_in(principal).
      4. daemon_main:
         - Add the --peek-release argument next to --revoke-scoped-tokens (:2405), with the help text in Design.
         - Dispatch it next to :2497.
         - Implement run_peek_release exactly as Design says, with the printed messages verbatim. It belongs near run_revoke_scoped_tokens (:2323) under its own "# --peek-release" banner comment, copying that banner style.
      5. Tests:
         - tests/unit/test_daemon_main.py:
           - test_org_server_starts_peek_provisioner_with_registry_evict (monkeypatch peek_provisioning.start and assert its arguments)
           - test_org_server_refuses_broken_peek_auto_provision (ConfigurationError)
           - TestPeekRelease, which runs against a fake PeekProvisioningClient, with these cases:
             - unknown account
             - not enabled
             - missing key
             - success deletes a provisioned token file and the state file
             - success keeps a device token file
             - 404 counts as released
             - client error → exit 1 with the message
         - tests/unit/web/test_routes_org_identity.py: a successful callback calls notify_sign_in once with the principal; a failed callback does not call it; notify_sign_in raising does not change the redirect (wrap the call so an exception is logged and ignored, and test that).
         - tests/unit/web/test_oauth_provider.py: handle_idp_callback calls notify_sign_in with the principal after the policy check; an AuthorizationDenied does not call it.
      6. Run ruff check ., python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py, and python3 -m pytest tests/unit/test_daemon_main.py tests/unit/web/test_routes_org_identity.py tests/unit/web/test_oauth_provider.py -q.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py -k "peek_provisioner or PeekRelease or peek_auto_provision" -q passes
      - python3 -m pytest tests/unit/web/test_routes_org_identity.py tests/unit/web/test_oauth_provider.py -k notify_sign_in -q passes
      - grep -n '"--peek-release"' src/privacyfence/daemon_main.py matches
      - python3 scripts/mypy_strict_modules.py passes

  - id: p5-connect-and-docs
    title: /connect hint and the user-facing docs
    depends_on: [p3-provisioner]
    complexity: S
    touches:
      - src/privacyfence/web/routes_connect.py
      - tests/unit/web/test_routes_connect.py
      - docs/peek-setup.md
      - docs/org-mode-setup-guide.md
    brief: |
      Read this plan's Design sections "/connect" and "Peek's provisioning API".
      1. In web/routes_connect.py _peek_box_html (:960-997), read the record once (it already calls load_token_file at :966). When connected and record["source"] == "provisioned", insert the exact field-help line from Design before the Reconnect form.
      2. tests/unit/web/test_routes_connect.py TestPeekBox (:725): a provisioned file shows the line; a device file does not.
      3. docs/peek-setup.md: add a section "## Automatic accounts (organization mode)" after "## Users connect". Cover:
         - what it does
         - that Peek never connects to PrivacyFence
         - the Peek admin creating a key with `peek provisioner create --name PrivacyFence`
         - putting it in the daemon's environment under PRIVACYFENCE_PEEK_PROVISIONING_KEY (or the name given to --peek-provisioning-key-env)
         - `--peek-auto-provision`
         - that a person's own sign-in wins
         - that Peek admins are not adopted and connect by hand
         - 90-day tokens renewed automatically
         - `--peek-release`
         - what each product does when the other is down
         Add every new message from this plan's Design (startup warning, --peek-release messages, the four PeekProvisioningClient errors) to "## Troubleshooting" with its fix. Add the `--peek-release` and bundle flags to "## Build and distribute the bundle".
      4. docs/org-mode-setup-guide.md:
         - In the connector table's Peek row and the Peek note (:255-256), add "or automatic accounts with --peek-auto-provision ([Peek setup](peek-setup.md#automatic-accounts-organization-mode))".
         - In the users/<principal> file table (:1061-1067), add a row for `users/<principal>/credentials/peek_provisioning.json`: "Peek auto-provisioning state and the person's email, for token renewal."
         - In the bundle option table (:341), add the two new options.
      5. Run ruff check . and python3 -m pytest tests/unit/web/test_routes_connect.py -q.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_connect.py -k Peek -q passes
      - grep -n "## Automatic accounts (organization mode)" docs/peek-setup.md matches
      - grep -n "peek_provisioning.json" docs/org-mode-setup-guide.md matches

  - id: p6-retire
    title: ADRs 0239 and 0240, changelog, delete the plan
    depends_on: [p4-wiring, p5-connect-and-docs]
    complexity: S
    touches:
      - docs/adr/0239-*.md
      - docs/adr/0240-*.md
      - docs/adr/README.md
      - CHANGELOG.md
      - docs/peek-auto-provisioning-plan.md
      - docs/peek-auto-provisioning-plan-manual-steps.html
    brief: |
      Read docs/adr/README.md (template and rules; take the next free numbers, 0239 and 0240, unless main has used them: then the next free ones, updating every reference).
      1. Write docs/adr/0239-privacyfence-provisions-peek-accounts-through-a-scoped-key-and-only-calls-peek.md:
         - Title and decision from this plan's ADRs section, Status Accepted.
         - Context: today's device sign-in (ADR 0151, 0152); the separate DMZs.
         - Decision; consequences; and the rejected alternatives from Design "Rejected alternatives" (Peek pulls or webhooks, admin key, key in bundle, synchronous sign-in, startup provisioning without email, disabling on release).
         - Link to source files (src/privacyfence/peek_provisioning.py, peek_client.py) and the Peek repository's docs/src/content/docs/auth-access.md, never to this plan.
      2. Write docs/adr/0240-a-persons-own-peek-sign-in-wins-over-provisioning.md: the source marker, 90/30-day renewal, the stored email and why (sweep renewal without a sign-in), and the rejected "overwrite the device sign-in".
      3. Add both rows to the index in docs/adr/README.md, in the existing format.
      4. CHANGELOG.md, under "## [Unreleased]" in the section the file uses for additions, one line:
         "Organization mode can create a Peek account for each person and connect it automatically (`--peek-auto-provision`, with a Peek provisioning key); `privacyfence-app --peek-release` revokes it when someone leaves."
         Never add a version heading.
      5. git rm docs/peek-auto-provisioning-plan.md docs/peek-auto-provisioning-plan-manual-steps.html, and grep that nothing links to them.
      6. Run ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - test ! -e docs/peek-auto-provisioning-plan.md && test ! -e docs/peek-auto-provisioning-plan-manual-steps.html
      - grep -rn "peek-auto-provisioning-plan" docs src README.md CHANGELOG.md prints nothing
      - ls docs/adr/0239-*.md docs/adr/0240-*.md lists two files
      - grep -n "0239" docs/adr/README.md matches
      - python3 -m pytest tests/unit -q passes
```
