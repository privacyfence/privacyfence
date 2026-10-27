# Plan: built-in OAuth apps, admin-managed services and an OAuth redirect relay

## Goal

Today a fresh PrivacyFence install offers no connector at all until an administrator registers
their own OAuth app with Google, Slack, Atlassian, Salesforce or Miro and ships the credentials in
a signed organization bundle (ADR 0196). This plan makes a fresh install work out of the box:

- Every distribution ships **built-in PrivacyFence-owned OAuth apps** for Google, Slack, Atlassian,
  Salesforce and Miro, baked in at build time like Telegram's app credentials (ADR 0040).
- A desktop install (local mode) uses them through the same fixed loopback redirects as today.
- An organization server (org mode) uses them through a new **PrivacyFence-hosted OAuth redirect
  relay** at `https://oauth.privacyfence.eu`. The relay forwards a provider's redirect only to an
  org server that has proven it controls its own domain.
- An administrator decides in **Settings → Apps** which services are offered, and can replace any
  built-in app with the organization's own client ID and secret. Desktop installs: the install
  owner, with step-up. Org servers: an IdP admin, with step-up.
- An organization bundle that already has a connector section keeps working unchanged. Its section
  wins and shows as locked in Settings.

Grist and Peek are unchanged. Their apps and servers live on each organization's own server, so
they cannot have a built-in app, and they stay bundle-only. Telegram already ships its app; it gains
only the admin "offered" switch.

## Current state

- **Which connectors are offered.** `org_mode.CONNECTOR_ORG_SECTION` and
  `org_mode.connector_enabled_by_org` (`src/privacyfence/org_mode.py:523-535`, ADR 0196) offer a
  connector only when `org_config.json` has a non-empty section for it. An install with no bundle
  offers nothing. Callers:
  - `daemon_main.build_connectors` (`daemon_main.py:1665`, `:1699`, `:1714`, `:1733`)
  - `settings_controller._org_allows` (`:1297`) and `_connectors_state` (`:2180`)
  - `web/routes_connect._is_configured` (`:175-192`), `telegram_start` (`:578`) and
    `_render_connect_page` (`:1036`)
- **Where app credentials are read.** All reads use the bundle sections `google` (`client_id`,
  `client_secret`, `auth_uri`, `token_uri`), `slack` (`client_id`, `client_secret`, optional
  `user_scopes`), `salesforce` (`consumer_key`, `consumer_secret`, optional `login_url`),
  `atlassian` (`client_id`, `client_secret`, optional `assets`, `assets_workspace_id`) and `miro`
  (`client_id`, `client_secret`):
  - `daemon_main._google_client_config` (`:1292`) wraps `google` as `{"installed": ...}` for local
    mode.
  - `google_oauth.web_client_config` (`google_oauth.py:57-67`) wraps it as `{"web": ...}` for org
    mode.
  - `build_connectors` (`:1354-1745`), the CLI runners `run_*_oauth` (`:1803-1994`), the
    `settings_controller._authenticate_*` methods (`:1302-1475`, local) and
    `web/routes_connect._build_authorize_url` / `_exchange_and_save` (`:333-453`, org).
- **Refresh.** Google token files embed `client_id`/`client_secret` (google-auth's `to_json`), so
  Google refreshes with the app that issued the token. Salesforce, Atlassian (Jira, Confluence,
  Assets) and Miro refresh with the client credentials of the *current* section, merged in by
  `build_connectors` (`:1552`, `:1577`, `:1735`). Slack never refreshes.
- **Redirects.**
  - Local mode (`oauth_loopback.run_browser_oauth`): Google `http://localhost:<any port>/` (Desktop
    client), Slack `http://127.0.0.1:53682/callback`, Salesforce `http://localhost:53683/callback`,
    Atlassian `http://127.0.0.1:53684/callback`, Miro `http://localhost:53686/callback`.
  - Org mode: `{issuer_url}/oauth/callback/<grant>`, where `<grant>` is `gmail`, `drive`,
    `calendar`, `contacts`, `tasks`, `apps_script`, `slack`, `salesforce`, `atlassian` or `miro`
    (`routes_connect.py:487`, `:527`).
  - Google needs a Desktop client for loopback and a Web client for HTTPS. An Atlassian app holds
    exactly one callback URL (`docs/atlassian-setup.md:16`).
- **Bundle loading.**
  - `daemon_main.load_org_config()` (`:451-529`) verifies the signature (ADR 0016, 0200).
  - It is called by `run_app` (`:2091`), by the CLI dispatch (`:2518`) and by
    `settings_controller._org_config_or_empty` (`:1203`) and `refresh_connectors` (`:1240`).
  - Org mode captures the dict once into `OrgAuth.org_config` (`web/server.py`, around `:710`). That
    one object reaches `routes_connect.build_routes`, `build_org_routes` (`web/server.py:1343-1421`)
    and `_connectors_for_principal` (`daemon_main.py:1192-1207`).
- **Built-in credentials precedent.** `scripts/telegram_credentials.py` writes the git-ignored
  `src/privacyfence/_telegram_credentials.py` from the `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`
  secrets. `MANIFEST.in:6` includes it in the sdist and wheel.
  - Callers: `scripts/build_dmg.sh:109`, `scripts/build_deb.sh:95`,
    `scripts/build_installer.ps1:94`, `.github/workflows/build.yml:791-806` (org image) and
    `.github/workflows/publish-pypi.yml:179-236`.
  - `check-dist --require` fails a stable build without it.
  - Reader: `app_credentials.telegram_app_credentials()`.
  - Tests: `tests/unit/test_telegram_credentials.py`.
- **Settings actions.**
  - Declared in `web/org_settings_scope.ACTION_SCOPES` (`:64-146`).
  - Local dispatch: `routes_settings.settings_action` (`:931-978`), where every local settings route
    is already owner-only (`web/server.py:814-854`).
  - Org dispatch: the hand-written `_ORG_ALLOWED_ACTIONS` (`:1434`) and `_apply_org_action`
    (`:1994-2057`).
  - Step-up: `_SENSITIVE_ACTIONS` / `_NON_SENSITIVE_ACTIONS` (`:226-268`), pinned by
    `TestSensitiveActionsCoverAllAllowedActions`.
  - Audit: `_record_settings_audit`, with decision `settings_change`.
- **Cloudflare.** `cloudflare/downloads/` is the one Worker (vitest with
  `@cloudflare/vitest-pool-workers`, a D1 database), deployed by
  `.github/workflows/deploy-download-worker.yml` with step-level `CF_DOWNLOADS_WORKER_*` secrets.
  Observability is off (ADR 0186).
- **Privacy page.** `website/privacy/index.html:26-27` and `:45-54` say the policy covers only the
  site and `downloads.privacyfence.eu`, and that the software "sends nothing to the project".
- **ADR numbers.** The highest accepted ADR is 0238, so this plan takes 0239 to 0242.

## Design

### D1. Built-in app credentials

Seven built-in app keys:

| Key | Used for | Redirects registered on the app (manual step mb1) |
|---|---|---|
| `google_desktop` | Google, local mode | none (Desktop app client) |
| `google_web` | Google, org mode via relay | `https://oauth.privacyfence.eu/callback/<g>` for `g` in gmail, drive, calendar, contacts, tasks, apps_script |
| `slack` | Slack, both modes | `http://127.0.0.1:53682/callback`, `https://oauth.privacyfence.eu/callback/slack` |
| `salesforce` | Salesforce, both modes | `http://localhost:53683/callback`, `https://oauth.privacyfence.eu/callback/salesforce` |
| `atlassian_local` | Atlassian, local mode | `http://127.0.0.1:53684/callback` |
| `atlassian_relay` | Atlassian, org mode via relay | `https://oauth.privacyfence.eu/callback/atlassian` |
| `miro` | Miro, both modes | `http://localhost:53686/callback`, `https://oauth.privacyfence.eu/callback/miro` |

Every app has two fields, `client_id` and `client_secret`.

**Generator: `scripts/builtin_apps.py`.** Stdlib only, modelled on `scripts/telegram_credentials.py`.

- `write` reads, for each key `K` above, the environment variables
  `PF_APP_<K upper>_CLIENT_ID` and `PF_APP_<K upper>_CLIENT_SECRET`, for example
  `PF_APP_GOOGLE_DESKTOP_CLIENT_ID`. Its behaviour:
  - It writes the git-ignored `src/privacyfence/_builtin_apps.py`, containing exactly one
    assignment: `APPS = <repr of a dict>`. The dict maps each key that has *both* values non-empty to
    `{"client_id": ..., "client_secret": ...}`, with the keys sorted.
  - A key with only one of its two values set prints
    `warning: PF_APP_<K>_CLIENT_ID and PF_APP_<K>_CLIENT_SECRET must both be set; skipping <k>.`
    and is left out.
  - For each key it prints `built-in app <k>: included` or `built-in app <k>: not set`. ASCII
    output only.
  - When no key has both values, it deletes any stale module and returns 0, like the Telegram
    generator.
  - A value containing a newline or a NUL character prints `error: <VAR> contains a control
    character.` to stderr and returns 2.
- `check-dist [--require] files...` is the same logic as `telegram_credentials.check_dist`, with
  `MODULE_SUFFIX = "privacyfence/_builtin_apps.py"` and the ADR number in the error message changed
  to 0239.

**Reader: `app_credentials.builtin_app(key: str) -> dict[str, str] | None`.**

- It does a literal `from . import _builtin_apps`, so that PyInstaller's static analysis bundles the
  module, exactly as `telegram_app_credentials` does. It returns a copy of `APPS.get(key)`.
- On `ImportError` it falls back to the env var `PRIVACYFENCE_BUILTIN_APPS`: the path of a JSON
  file with the same `{key: {"client_id", "client_secret"}}` shape, for development. A missing,
  unreadable or malformed file returns `None` and logs one warning without the file content.
- It also exposes `BUILTIN_APP_KEYS: tuple[str, ...]`, the seven keys in the table order above.

The built-in client secrets are public in effect, just as Telegram's hash is (ADR 0040). This is
accepted: each one identifies the PrivacyFence app, and every sign-in still needs the person's own
consent.

### D2. Admin settings file

The file is `paths.org_dir() / "admin_settings.json"`. The daemon is its only writer, through
`secure_files.atomic_write_json` (mode 0600). It sits outside the signed bundle: the bundle is
signed offline, so the daemon cannot re-sign it.

Schema (version 1):

```json
{"version": 1,
 "services": {
   "google":     {"offered": true, "app": {"client_id": "…", "client_secret": "…"}},
   "slack":      {"offered": false},
   "salesforce": {"offered": true, "app": {"consumer_key": "…", "consumer_secret": "…", "login_url": "https://acme.my.salesforce.com"}},
   "atlassian":  {"offered": true},
   "miro":       {"offered": true},
   "telegram":   {"offered": true}}}
```

- `MANAGED_SERVICES = ("google", "slack", "atlassian", "salesforce", "miro", "telegram")`, in that
  order. These are the bundle section names from `CONNECTOR_ORG_SECTION`.
- `offered` defaults to `true` when the file is missing, the service is missing, or the key is
  missing. Decision: a fresh install offers everything it has an app for.
- `app` is allowed for every managed service except `telegram`. Its fields per service:
  - google, slack, atlassian, miro: `client_id`, `client_secret`
  - salesforce: `consumer_key`, `consumer_secret`, optional `login_url`
- Validation: `client_id`/`consumer_key` is 1-512 printable ASCII characters with no spaces, and the
  secret is 1-512 printable ASCII characters with no spaces. `login_url` must be
  `https://<host>` with no path other than an empty path or `/`.

**Module `src/privacyfence/admin_settings.py`:**

- `class AdminSettingsError(ValueError)`.
- `load() -> dict[str, Any]`:
  - A missing file gives `{"version": 1, "services": {}}`.
  - Unreadable or malformed JSON, a non-object, or `version != 1` raises `AdminSettingsError`,
    whose message names the path and never the content.
- `save(data: dict[str, Any]) -> None` writes atomically, mode 0600.
- `offered(data, service) -> bool`.
- `own_app(data, service) -> dict[str, str] | None`.
- `apply_change(action: str, *, service: str, client_id: str = "", client_secret: str = "", login_url: str = "") -> str`:
  - It loads, validates, mutates, saves, and returns the audit summary.
  - Actions and their summaries:
    - `offer_service`: `Offered <Label> to everyone on this install`
    - `withdraw_service`: `Stopped offering <Label>`
    - `set_service_app`: `Set <Label> to use the organization's own app (client <client_id>)`
    - `clear_service_app`: `Set <Label> back to the built-in PrivacyFence app`
  - `<Label>` comes from `SERVICE_LABELS = {"google": "Google", "slack": "Slack", "atlassian":
    "Atlassian", "salesforce": "Salesforce", "miro": "Miro", "telegram": "Telegram"}`.
  - For `set_service_app` on salesforce, the form's `client_id`/`client_secret` are stored as
    `consumer_key`/`consumer_secret`, and `login_url` is stored only when it is non-empty.
  - Errors raise `AdminSettingsError` with these exact texts:
    - `Unknown service.`
    - `Telegram's app is built in and cannot be replaced.`
    - `Enter both the client ID and the client secret.`
    - `The client ID or secret contains characters that are not allowed.`
    - `The login URL must be https://<your domain> with no path.`
  - The secret never appears in any message, log line or return value.
  - A `threading.Lock` (module level, `_write_lock`) serializes `apply_change`.

### D3. The effective org config

**Module `src/privacyfence/oauth_apps.py`.** It is pure apart from `app_credentials.builtin_app`.

- `RELAY_BASE_URL = "https://oauth.privacyfence.eu"`
- `RELAY_MARKER = "relay"`: a key in a synthesized section.
- `GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"`
- `GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"`
- `BUILTIN_KEY = {("google", "local"): "google_desktop", ("google", "org"): "google_web", ("slack", "local"): "slack", ("slack", "org"): "slack", ("salesforce", "local"): "salesforce", ("salesforce", "org"): "salesforce", ("atlassian", "local"): "atlassian_local", ("atlassian", "org"): "atlassian_relay", ("miro", "local"): "miro", ("miro", "org"): "miro"}`
- `AppSource = Literal["bundle", "admin", "builtin", "none"]`
- `def resolve_source(bundle: dict[str, Any], admin: dict[str, Any], service: str, *, mode: Mode) -> AppSource`
- `def effective_org_config(bundle: dict[str, Any], admin: dict[str, Any], *, mode: Mode) -> dict[str, Any]`
  - It returns a **new** dict: a shallow copy of `bundle`, plus a section for each managed service
    the bundle has no non-empty section for.
  - It also strips `RELAY_MARKER` from every bundle section, so a bundle cannot opt into the relay.
  - It never mutates `bundle` or `admin`.
  - The section for a managed service, in this order:
    1. **Bundle section non-empty**: keep it unchanged (source `bundle`). The `offered` toggle does
       not apply; the UI shows the row locked.
    2. **Not `offered`**: no section (source `none`).
    3. **`telegram`**: `{"enabled": True}` when `app_credentials.telegram_app_credentials()` is not
       None (source `builtin`), otherwise none.
    4. **Admin `app` present**: the app fields (source `admin`). For google, also `auth_uri` and
       `token_uri` with the constants above.
    5. **`builtin_app(BUILTIN_KEY[(service, mode)])` present**: source `builtin`. For salesforce the
       section is `{"consumer_key", "consumer_secret"}`; for google it adds `auth_uri`/`token_uri`;
       otherwise it is `{"client_id", "client_secret"}`. In `mode == "org"` it also adds
       `"relay": True`.
    6. **Otherwise**: none (source `none`).
- `def refresh_in_place(target: dict[str, Any], new: dict[str, Any]) -> None` deletes the keys of
  `target` that are not in `new`, then `target.update(new)`.
  - It is used in org mode, where many objects hold the one `org_config` dict.
  - Each key assignment is atomic under the GIL. A request that reads during a refresh sees either
    the old or the new section for each service, and that is accepted.
- `def relay_redirect_uri(grant: str) -> str` returns `f"{RELAY_BASE_URL}/callback/{grant}"`.
- `def uses_relay(section: dict[str, Any]) -> bool` returns `section.get(RELAY_MARKER) is True`.

**Where it is applied: `daemon_main.effective_org_config(bundle: dict[str, Any]) -> dict[str, Any]`.**

- It returns
  `oauth_apps.effective_org_config(bundle, admin_settings.load(), mode=org_mode.resolve_mode(bundle))`.
- When `admin_settings.load()` raises `AdminSettingsError`, it is re-raised as
  `org_mode.ConfigurationError(str(exc))`, so a broken file refuses to start, like a broken bundle.
- Call sites:
  - `run_app` (`daemon_main.py:2091`) keeps `bundle = load_org_config()`. It sets
    `org_config = effective_org_config(bundle)` right after `reject_removed_tool_annotations(bundle)`.
  - `log_org_config_bundle_hash(bundle)` keeps the raw bundle.
  - Everything else in `run_app` takes `org_config`.
  - The CLI dispatch at `:2518` uses `effective_org_config(load_org_config())`.
  - `settings_controller._org_config_or_empty` (`:1203`) and `refresh_connectors`'s `work`
    (`:1240`) call `effective_org_config(load_org_config())`.
  - The QA scripts (`scripts/qa_*.py`) are not changed. They run against the runner's own bundle.
- In org mode, `run_app` passes the raw `bundle` down to `_start_org_web_server`, which stores it on
  `OrgAuth` as a new field `bundle: dict[str, Any]`. `build_org_routes` gets a new keyword
  `bundle: dict[str, Any]`, so an admin change can recompute the effective config.

Nothing else changes in how connectors read their sections. `connector_enabled_by_org`, the
`_authenticate_*` methods, `build_connectors` and `routes_connect` keep reading `org_config`, which
is now the effective one.

### D4. Settings → Apps (both modes)

**New actions in `web/org_settings_scope.ACTION_SCOPES`.** All of them use
`ActionScope(frozenset({LOCAL_MODE, ORG_MODE}), admin_only=True)`:

| Action | Parameters | `_SENSITIVE_ACTIONS`? |
|---|---|---|
| `offer_service` | `service: str` | yes (ADR 0070: enabling is sensitive) |
| `withdraw_service` | `service: str` | no |
| `set_service_app` | `service: str, client_id: str, client_secret: str, login_url: str` | yes |
| `clear_service_app` | `service: str` | yes |

**Local mode.**

- Add four `SettingsController` methods with exactly those parameters. Each one:
  - calls `admin_settings.apply_change(...)`;
  - on `AdminSettingsError` sets `self.error = str(exc)`;
  - on success calls `self.refresh_connectors()`;
  - returns `self.snapshot()`.
- Settings routes are already owner-only (`_owner_only_routes`), so this is the "install owner"
  rule.
- Step-up applies through the existing gate.
- The audit summary is the existing generic `Changed setting '<action>' (principal=local)`, which
  carries no payload.

**Org mode.**

- Add the four actions to `_ORG_ALLOWED_ACTIONS`.
- Add a branch to `_apply_org_action` that:
  1. calls `admin_settings.apply_change(...)`;
  2. on success calls
     `oauth_apps.refresh_in_place(org_config, oauth_apps.effective_org_config(bundle, admin_settings.load(), mode="org"))`
     on the shared `org_config` dict;
  3. calls the new `connector_registry.evict_all()`;
  4. returns the summary from `apply_change` suffixed with `(admin=<id>)`, as the existing admin
     branches do.
- An `AdminSettingsError` becomes the same 400 error shape the existing branches use for a bad
  payload.

**`ConnectorRegistry.evict_all() -> None`** (`connector_registry.py`) clears `_hosts` and
`_last_used` under the lock.

**Snapshot rows.** Add `admin_settings.service_rows(bundle, admin, *, mode) -> list[dict[str, Any]]`.
It returns one row per managed service, in `MANAGED_SERVICES` order, with these fields:

| Field | Value |
|---|---|
| `key` | the service name |
| `label` | from `SERVICE_LABELS` |
| `offered` | `bool` |
| `source` | `"bundle"`, `"admin"`, `"builtin"` or `"none"`, from `oauth_apps.resolve_source` |
| `locked` | `source == "bundle"` |
| `client_id` | the effective client id or consumer key, `""` for telegram/none |
| `builtin_available` | whether the built-in app for this mode exists in the build (telegram: `telegram_app_credentials() is not None`) |
| `relay` | `mode == "org" and source == "builtin" and service != "telegram"` |
| `replaceable` | `service != "telegram"` |

- No row ever carries a secret.
- The local snapshot adds `"services"` from these rows.
- The org `_org_state` adds `"services"` when `principal.is_admin` (otherwise `[]`), plus
  `"relay_status": relay_client.status()` (D5) when `principal.is_admin`.

**Capabilities.** `_capabilities_for` gains `apps`: `True` in local mode and `is_admin` in org mode.

**UI: `settings_window_html.py`.** A new nav section **Apps**, shown when `CAPS.apps`, rendered by
`renderApps(state)`.

- One card per row:
  - The label.
  - A toggle `offer_service`/`withdraw_service`, disabled when `locked`.
  - A status line:
    - bundle: `Set by your organization's configuration file.`
    - admin: `Uses your organization's own app (client <client_id>).`
    - builtin, local: `Uses the built-in PrivacyFence app.`
    - builtin with relay: `Uses the built-in PrivacyFence app through oauth.privacyfence.eu.`
    - none and offered and not `builtin_available`: `No app available: this build has no built-in app. Enter your organization's own.`
    - not offered: `Not offered.`
  - When `replaceable` and not `locked`, a "Use your own app" form with Client ID and Client secret
    inputs (`type="password"`, `autocomplete="off"`). It adds a "Login URL (optional)" input for
    salesforce. It posts `set_service_app`.
  - A "Use the built-in app" button that posts `clear_service_app`, shown when `source ==
    "admin"`.
- In org mode, a relay status line above the cards:
  - `Relay registration: registered <time>.`, or
  - `Relay registration failed: <detail>. Built-in apps cannot sign in until it succeeds.`, or
  - `Relay registration: not needed.` when no row has `relay`.

### D5. Org mode through the relay

**Contract between the org server and the Worker.** Both phases implement exactly this.

- `host` is `urlsplit(issuer_url).netloc.lower()`, for example `pf.acme.example.com` or
  `pf.acme.example.com:8443`. Regex: `^[a-z0-9.-]{1,253}(:[0-9]{1,5})?$`.
- The well-known token is 43 characters of `[A-Za-z0-9_-]` (`secrets.token_urlsafe(32)`).
- `state` for a relayed sign-in is `f"{host}~{secrets.token_urlsafe(32)}"`.

**Org server side: new module `src/privacyfence/relay_client.py`.**

- `well_known_token() -> str` reads `paths.org_dir() / "relay_token.txt"`, or creates it with
  `atomic_write_text` (mode 0600) when it is missing or not 43 valid characters.
- `register(issuer_url: str) -> tuple[bool, str]` does
  `requests.post(f"{RELAY_BASE_URL}/register", json={"host": host, "token": well_known_token()}, timeout=10, allow_redirects=False)`.
  - 200 gives `(True, "")`.
  - Otherwise `(False, detail)`, where `detail` is the response JSON's `error` string when it is one
    of the Worker's codes below, `HTTP <status>` otherwise, or `could not reach
    oauth.privacyfence.eu` on `requests.RequestException`.
- `status() -> dict[str, Any]` returns `{"last_ok_at": float | None, "last_error": str,
  "needed": bool}` from module state. It is reset by a new `relay_client.reset_for_tests()` called
  from `tests/conftest.py::_reset`.
- `start_registration_loop(issuer_url: str, org_config: dict[str, Any], stop: threading.Event) -> threading.Thread`
  starts a daemon thread that checks every 3600 s, starting immediately:
  - `needed` = any section in `org_config` with `oauth_apps.uses_relay(section)`.
  - When needed and (`last_ok_at` is None or older than 86400 s, or `last_error` is set), it calls
    `register` and updates the state.
  - It logs at INFO on success and at WARNING with `detail` on failure, never the token.
  - `_start_org_web_server` starts it with the shared `org_config` and the daemon's existing
    shutdown event.

**Well-known route.**

- Org mode only, in `web/server.py` next to the other org routes:
  `GET /.well-known/privacyfence-relay`.
- It is unauthenticated and returns `PlainTextResponse(relay_client.well_known_token(),
  headers={"Cache-Control": "no-store"})`.
- It is a public route by design: the token proves control of the domain and grants nothing.

**`web/routes_connect.py`.**

- In `start`, when the resolved section for `service` satisfies `oauth_apps.uses_relay(section)`:
  - `redirect_uri = oauth_apps.relay_redirect_uri(_GRANT_KEY[service])`;
  - `state = f"{netloc}~{secrets.token_urlsafe(32)}"`, with `netloc` computed once from
    `issuer_url`.
- Otherwise both are unchanged.
- `callback` recomputes the same `redirect_uri` from the popped pending entry's service for the
  token exchange. Add a `redirect_uri: str` field to the pending entry, set in `start` and reused
  in `callback`, rather than recomputing it.
- Add a helper `_section_for(service, org_config) -> dict[str, Any]` that maps a service to its
  `org_config` section through `org_mode.CONNECTOR_ORG_SECTION`.
- In `_build_authorize_url` and `_exchange_and_save`, the Google branch passes
  `{k: v for k, v in section.items() if k != oauth_apps.RELAY_MARKER}` to
  `google_oauth.web_client_config`, so the marker never reaches google-auth's client config. The
  other providers read named fields only and need no change.

**The Worker: `cloudflare/oauth-relay/`.** TypeScript, a copy of `cloudflare/downloads`' tooling
(`package.json` scripts `test`, `typecheck` and `dry-run`; the same devDependency versions; vitest
with `@cloudflare/vitest-pool-workers`).

- `wrangler.toml`:
  - `name = "privacyfence-oauth-relay"`, `main = "src/index.ts"`, `compatibility_date =
    "2026-08-22"`
  - `workers_dev = false`
  - `[[routes]] pattern = "oauth.privacyfence.eu"`, `custom_domain = true`
  - `[observability] enabled = false`
  - One SQLite-backed Durable Object: `[[durable_objects.bindings]] name = "REGISTRY"`,
    `class_name = "Registry"`, `[[migrations]] tag = "v1"`,
    `new_sqlite_classes = ["Registry"]`
  - A Durable Object needs no dashboard-provisioned ID, unlike D1.
- Registry table:
  `registrations(host TEXT PRIMARY KEY, registered_at TEXT NOT NULL, verified_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)`,
  accessed through the single instance `env.REGISTRY.idFromName("global")`.
- Routes (anything else returns 404):
  - `GET /health` → `200 ok`.
  - `POST /register`, JSON body `{host, token}`:
    - Validate `host` against the regex and `token` against `^[A-Za-z0-9_-]{43}$`. Failure gives
      400 `{"error":"invalid_request"}`.
    - A host matching `localhost`, an IP literal, or a name ending in `.local`, `.internal` or
      `.localhost` gives 400 `{"error":"host_not_public"}`.
    - A revoked host gives 403 `{"error":"revoked"}`.
    - Fetch `https://<host>/.well-known/privacyfence-relay` with `redirect: "manual"`, a 5 s
      timeout (`AbortSignal.timeout(5000)`) and the body read up to 256 bytes. A non-200 response,
      or a trimmed body different from `token`, gives 403 `{"error":"proof_failed"}`.
    - On success, upsert `registered_at` (kept from the first registration) and `verified_at =
      now`, then return 200 `{"host", "verified_at"}`.
  - `GET /callback/:grant`, where `grant` is in `gmail`, `drive`, `calendar`, `contacts`, `tasks`,
    `apps_script`, `slack`, `salesforce`, `atlassian` or `miro`:
    - Read `state`, split at the first `~`, and validate the host part with the regex.
    - The registration must exist, not be revoked, and have `verified_at` within the last 35 days.
    - Then return a 302 to `https://<host>/oauth/callback/<grant>?<the original query string,
      unchanged>`.
    - Otherwise return 400 `text/plain`: `This sign-in cannot be completed: the PrivacyFence server
      it belongs to is not registered with the PrivacyFence relay. Ask your administrator.`
    - The Worker never stores or logs `code`.
  - `GET /registrations` → 200 JSON `[{"host", "registered_at"}]` for non-revoked hosts, sorted by
    host. This is the public listing the maintainer chose.
  - `POST /admin/revoke` with `Authorization: Bearer <REVOKE_TOKEN>` (a Worker secret, compared in
    constant time) and JSON body `{host}`:
    - It sets `revoked = 1`, inserting a revoked row when the host is absent.
    - It returns 200 `{"host", "revoked": true}`.
    - A missing or wrong token gives 401 `{"error":"unauthorized"}`.
    - The deploy workflow sets the secret from the GitHub environment secret
      `CF_OAUTH_RELAY_REVOKE_TOKEN`: a step running `npx wrangler secret put REVOKE_TOKEN` with the
      value on stdin and the secret in step-level `env:` only.
- Every response carries `Cache-Control: no-store`, `Referrer-Policy: no-referrer` and
  `X-Content-Type-Options: nosniff`.
- `.github/workflows/deploy-oauth-relay-worker.yml` is a copy of `deploy-download-worker.yml` with:
  - paths `cloudflare/oauth-relay/**` and the workflow file itself;
  - `working-directory: cloudflare/oauth-relay`;
  - environment `oauth-relay-worker`;
  - secrets `CF_OAUTH_RELAY_WORKER_API_TOKEN` and `CF_OAUTH_RELAY_WORKER_ACCOUNT_ID`;
  - no D1 migrations step; instead, after "Deploy Worker", a "Set revoke token" step that runs
    `printf '%s' "$REVOKE_TOKEN" | npx wrangler secret put REVOKE_TOKEN`, with step-level env
    `REVOKE_TOKEN: ${{ secrets.CF_OAUTH_RELAY_REVOKE_TOKEN }}` plus the two Cloudflare secrets;
  - smoke test `curl --head https://oauth.privacyfence.eu/health`.

**Accepted risks**, recorded in ADR 0242:

- Slack documents no PKCE, so a relayed Slack code passes through the Worker in the browser
  redirect. The Worker keeps no logs and the code is single-use.
- Anyone who owns a public domain can register a server and run sign-ins under the PrivacyFence
  name. Domain proof makes this cost a domain and a running server, and revocation removes a
  reported host.
- An org server not reachable from the internet, for example one restricted by IP address (setup
  guide §7), cannot register. It must use its own apps.

## ADRs

- **0239** — Built-in PrivacyFence OAuth apps for Google, Slack, Atlassian, Salesforce and Miro
  ship in every distribution, baked in from CI secrets by `scripts/builtin_apps.py`. Relates 0040.
- **0240** — An app's credentials resolve as bundle section → admin settings → built-in app.
  Which services are offered is an admin switch in Settings, default on, whenever the bundle has no
  section for the service. Amends 0196.
- **0241** — Admin settings live in a daemon-written `org/admin_settings.json` outside the signed
  bundle. They are changed only by the install owner (local) or an IdP admin (org), with step-up,
  and a client secret is never sent back to a browser. Relates 0016, 0034, 0070.
- **0242** — Org servers use the built-in apps through the PrivacyFence-hosted redirect relay at
  `oauth.privacyfence.eu`. It forwards only to hosts that proved control of their domain, keeps no
  logs, lists registrations publicly, and maintainers can revoke a host. Relates 0186, 0011.

## Manual steps

Step-by-step page: https://claude.ai/artifact/TumFGa3FRUqYkvuQyuTkGB

- **Before:**
  - (mb1) Register the seven PrivacyFence-owned apps and store their 14 values as GitHub
    repository secrets.
  - (mb2) Create the Cloudflare API token and the GitHub environment for the relay Worker.
  - (mb3) Optionally, start Google's OAuth verification. It takes weeks and does not block the
    plan.
- **After:**
  - (ma1) Sign in to each provider from a desktop build of the branch using the built-in apps.
  - (ma2) Deploy the relay from the branch and sign in to each provider from an org server through
    it.
  - (ma3) Check that an override and a withdrawn service behave as described.

## Risks and open questions

- **The bundle may not be the only thing that makes `org_config` non-empty.** Some code may treat
  a non-empty `org_config` as "has a bundle". The planner's grep found only mode-based checks, but
  p3 greps again. If a truthiness check on `org_config` decides something other than connector
  sections, p3 stops (`status=blocked`).
- **Org-mode objects may copy `org_config`** instead of sharing the one dict, for example with
  `dict(org_config)` or `{**org_config}`. Then `refresh_in_place` does not reach them. p4 greps
  `src/privacyfence/web` and `daemon_main.py` for copies of the org config handed to long-lived
  objects. If one feeds the connect routes or `build_connectors`, p4 stops.
- **The setup.py build hook.** ADR 0237 added a build hook to `setup.py`. If the wheel's file list
  is now decided there rather than by `MANIFEST.in`, p1's "packaged" test needs the same path the
  Telegram module takes. p1 copies whatever the Telegram module does and stops if the two differ.
- **Provider constraints the code cannot enforce.** These are documented in p7, not built:
  - Google's unverified-app screen and its 100-user cap until verification.
  - Salesforce orgs block uninstalled connected apps by default.
  - Slack workspaces may need admin approval.
- **`google_web` for an org server's own app.** An override in org mode must be a Web application
  client with the org's own redirects. The UI copy and the docs say so; nothing in code checks the
  client type.

## Implementation manifest

```yaml
plan_slug: builtin-oauth-apps
feature_branch: feature/builtin-oauth-apps
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/TumFGa3FRUqYkvuQyuTkGB
manual_steps_source: docs/builtin-oauth-apps-plan-manual-steps.html
manual_before:
  - id: mb1-register-builtin-apps
    title: Register the seven PrivacyFence-owned OAuth apps and store their credentials as repository secrets
    why: p1 bakes them into every build and ma1/ma2 sign in with them; without them a build has no built-in apps and only the dev JSON fallback works
    done_when: Settings → Secrets and variables → Actions lists the 14 secrets PF_APP_GOOGLE_DESKTOP_CLIENT_ID/_SECRET, PF_APP_GOOGLE_WEB_CLIENT_ID/_SECRET, PF_APP_SLACK_CLIENT_ID/_SECRET, PF_APP_SALESFORCE_CLIENT_ID/_SECRET, PF_APP_ATLASSIAN_LOCAL_CLIENT_ID/_SECRET, PF_APP_ATLASSIAN_RELAY_CLIENT_ID/_SECRET, PF_APP_MIRO_CLIENT_ID/_SECRET
  - id: mb2-relay-worker-cloudflare
    title: Create the Cloudflare API token and the oauth-relay-worker GitHub environment for the relay Worker
    why: the relay Worker's deploy workflow (p6) needs them on main, and ma2 deploys from the branch with the same token
    done_when: the repository has an environment named oauth-relay-worker holding secrets CF_OAUTH_RELAY_WORKER_API_TOKEN, CF_OAUTH_RELAY_WORKER_ACCOUNT_ID and CF_OAUTH_RELAY_REVOKE_TOKEN, and the API token verifies with the curl in the page
  - id: mb3-google-verification
    title: (Optional, long-running) Submit the google_web and google_desktop clients' consent screen for Google verification
    why: until verified, Google shows an unverified-app screen and caps the app at 100 users; nothing in the plan waits for it
    done_when: the Google Cloud console's Verification Center shows the submission, or the user chose to defer it
manual_after:
  - id: ma1-desktop-builtin-signin
    title: On a desktop build of the feature branch with no organization bundle, sign in to Google (Gmail), Slack, Salesforce, Jira and Miro with the built-in apps
    why: real provider consent screens and loopback redirects cannot run in CI
  - id: ma2-org-relay-signin
    title: Deploy the relay from the feature branch, then on a test org server with no connector sections in its bundle, register with the relay and connect Gmail, Slack, Salesforce, Jira and Miro through it
    why: the relay round trip needs a public domain, the real Worker and real providers
  - id: ma3-override-and-withdraw
    title: In Settings → Apps, replace one built-in app with your own and withdraw one service, on both a desktop install and the org server
    why: checks the admin path end to end, with step-up, against a real provider
verify_after_merge:
  - 'python3 -m pytest tests/unit/test_builtin_apps_script.py tests/unit/test_app_credentials.py tests/unit/test_telegram_credentials.py -q'
  - 'python3 -m pytest tests/unit/test_admin_settings.py tests/unit/test_oauth_apps.py tests/unit/test_relay_client.py -q'
  - 'python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_connect.py tests/unit/web/test_routes_settings.py tests/unit/web/test_org_settings_scope.py tests/unit/web/test_server.py -q'
final_checks:
  - 'docs/builtin-oauth-apps-plan.md and docs/builtin-oauth-apps-plan-manual-steps.html are deleted and `grep -rn "builtin-oauth-apps-plan" --include=*.md --include=*.html --include=*.py --include=*.yml .` finds nothing'
  - 'ADRs 0239, 0240, 0241 and 0242 exist, are Accepted, and are listed in docs/adr/README.md''s index; ADR 0196''s and ADR 0040''s Status sections carry one line pointing at 0240 and 0239'
  - 'CHANGELOG.md has the entries under "## [Unreleased]" and no new version heading'
  - '"cd cloudflare/oauth-relay && npm ci && npm test && npm run typecheck && npm run dry-run" passes'
phases:
  - id: p1-builtin-apps-generator
    title: Bake built-in OAuth app credentials into every distribution and read them at runtime
    depends_on: []
    complexity: M
    touches:
      - scripts/builtin_apps.py
      - tests/unit/test_builtin_apps_script.py
      - src/privacyfence/app_credentials.py
      - tests/unit/test_app_credentials.py
      - .gitignore
      - MANIFEST.in
      - scripts/build_dmg.sh
      - scripts/build_deb.sh
      - scripts/build_installer.ps1
      - .github/workflows/build.yml
      - .github/workflows/publish-pypi.yml
    brief: |
      Read the plan's "Design → D1" section first; every name, env var, message and module format is there.
      1. Create scripts/builtin_apps.py by copying the structure of scripts/telegram_credentials.py (stdlib only, REPO_ROOT, a render(), write(creds_path, environ), archive_members/contains_credentials/check_dist, argparse main with `write` and `check-dist [--require] files*`). KEYS = ("google_desktop", "google_web", "slack", "salesforce", "atlassian_local", "atlassian_relay", "miro"); env vars PF_APP_<KEY.upper()>_CLIENT_ID / _CLIENT_SECRET. render(apps) returns exactly "APPS = " + repr(dict(sorted(apps.items()))) + "\n". Messages exactly as D1 says; ASCII only. check-dist's error line says "A stable release must ship it (ADR 0239)."
      2. Add `src/privacyfence/_builtin_apps.py` to .gitignore directly below the `_telegram_credentials.py` line, and `include src/privacyfence/_builtin_apps.py` to MANIFEST.in directly below line 6, extending the comment above to say both generated modules.
      3. In src/privacyfence/app_credentials.py add BUILTIN_APP_KEYS and builtin_app(key) per D1 (literal `from . import _builtin_apps` with the same `# type: ignore[attr-defined]` comment the Telegram import carries; the PRIVACYFENCE_BUILTIN_APPS JSON-file fallback; one logger.warning naming only the env var on a bad file). Extend the module docstring by one paragraph naming ADR 0239.
      4. Call the generator next to every Telegram generator call: scripts/build_dmg.sh (after line 109, `"$PYTHON" scripts/builtin_apps.py write`), scripts/build_deb.sh (after line 95), scripts/build_installer.ps1 (after lines 94-95, with its own `if ($LASTEXITCODE -ne 0) { throw "Writing built-in app credentials failed" }`).
      5. .github/workflows/build.yml: add the 14 `PF_APP_*: ${{ secrets.PF_APP_* }}` env entries to the step-level `env:` of the `build` job's "Build .app, .pkg, .mcpb and DMG" step, the `build-windows` "Build installer" step and the `build-deb` "Build .deb" step. In `build-org-image`, add a step "Write built-in app credentials" right after "Write Telegram app credentials" (same shape, the 14 env entries, `python3 scripts/builtin_apps.py write`) and a step "Check built-in app credentials are packaged" right after the Telegram check step (same REQUIRE logic, `python3 scripts/builtin_apps.py check-dist "${REQUIRE[@]}" dist/*.whl`).
      6. .github/workflows/publish-pypi.yml `build` job: the same two steps next to the Telegram ones, with check-dist over `dist/*.whl dist/*.tar.gz`. Secrets go only to the write step's env, never to the job.
      7. Tests: tests/unit/test_builtin_apps_script.py mirroring every test in tests/unit/test_telegram_credentials.py (generates an importable module whose APPS equals the input; a value with quotes/backslashes stays inert (ast.literal_eval of the right-hand side equals the input); one-of-two set warns and skips; none set deletes a stale module; a newline in a value returns 2 and writes nothing; ASCII output; check-dist pass/require-fail/warn/no-files/unknown-archive; main dispatch; git-ignored-but-packaged; build scripts, build.yml and publish-pypi.yml call `scripts/builtin_apps.py write`, publish-pypi.yml and build.yml call `scripts/builtin_apps.py check-dist`, and none contains `APPS = `). tests/unit/test_app_credentials.py: builtin_app returns the generated module's entry (monkeypatch sys.modules["privacyfence._builtin_apps"]), returns a copy, falls back to the JSON file, returns None and warns once for a missing/malformed file without logging its content, and BUILTIN_APP_KEYS order.
      8. Run `ruff check .` and `python3 -m pytest tests/unit/test_builtin_apps_script.py tests/unit/test_app_credentials.py tests/unit/test_telegram_credentials.py -q`.
      Stop condition: if `git grep -n "_telegram_credentials" setup.py` shows the setup.py build hook (ADR 0237) handles the Telegram module, mirror it for _builtin_apps; if the Telegram module is packaged by any mechanism you cannot mirror, stop with status=blocked.
    acceptance:
      - 'python3 -m pytest tests/unit/test_builtin_apps_script.py tests/unit/test_app_credentials.py tests/unit/test_telegram_credentials.py -q passes'
      - 'grep -c "scripts/builtin_apps.py write" .github/workflows/build.yml .github/workflows/publish-pypi.yml scripts/build_dmg.sh scripts/build_deb.sh scripts/build_installer.ps1 reports at least 1 for each file'
      - 'grep -n "src/privacyfence/_builtin_apps.py" .gitignore MANIFEST.in matches in both files'
      - 'ruff check . passes'
  - id: p2-admin-settings-and-resolution
    title: Admin settings store and the effective org config resolver
    depends_on: [p1-builtin-apps-generator]
    complexity: M
    touches:
      - src/privacyfence/admin_settings.py
      - src/privacyfence/oauth_apps.py
      - tests/unit/test_admin_settings.py
      - tests/unit/test_oauth_apps.py
    brief: |
      Read "Design → D2" and "D3" (the oauth_apps part only); every constant, signature, message and rule is there. These are two new pure modules; no caller changes in this phase.
      1. Create src/privacyfence/oauth_apps.py with exactly the constants and functions in D3 (RELAY_BASE_URL, RELAY_MARKER, GOOGLE_AUTH_URI, GOOGLE_TOKEN_URI, BUILTIN_KEY, AppSource, resolve_source, effective_org_config, refresh_in_place, relay_redirect_uri, uses_relay). Import Mode from org_mode, builtin_app and telegram_app_credentials from app_credentials, and MANAGED_SERVICES/offered/own_app from admin_settings. Order of the per-service rules is exactly D3's 1-6.
      2. Create src/privacyfence/admin_settings.py with exactly D2's names (AdminSettingsError, MANAGED_SERVICES, SERVICE_LABELS, load, save, offered, own_app, apply_change, _write_lock) and the path paths.org_dir() / "admin_settings.json" resolved at call time (so tests can monkeypatch paths.data_dir). Use secure_files.atomic_write_json. Error texts and summaries verbatim from D2. Also add service_rows(bundle, admin, *, mode) per D4's row table (it calls oauth_apps.resolve_source and reads the effective section's client_id/consumer_key from oauth_apps.effective_org_config(bundle, admin, mode=mode)).
         To avoid an import cycle, admin_settings must not import oauth_apps at module level: import it inside service_rows.
      3. Tests, tests/unit/test_oauth_apps.py: a parametrized table over (bundle section present / not offered / admin app / builtin present / builtin absent) × mode (local, org) × service asserting the resulting section and resolve_source; bundle section kept verbatim and its "relay" key stripped; google sections carry auth_uri/token_uri; salesforce builtin uses consumer_key/consumer_secret; org builtin sections carry "relay": True and local ones do not; telegram gets {"enabled": True} only when telegram_app_credentials() is not None; inputs are not mutated; refresh_in_place removes missing keys and updates others on the same object; relay_redirect_uri("slack") == "https://oauth.privacyfence.eu/callback/slack". Monkeypatch app_credentials.builtin_app / telegram_app_credentials as the module imports them.
         tests/unit/test_admin_settings.py: missing file defaults; malformed/non-object/wrong version raise AdminSettingsError without the content in the message; each apply_change action's stored result and returned summary; salesforce field mapping and optional login_url; every error text in D2; telegram set_service_app refused; the secret appears in no exception message, no log record (caplog) and no returned value; saved file mode is 0o600 on POSIX (skip on Windows the same way existing secure_files tests do); service_rows fields for each source, never containing a client_secret key.
      4. Run ruff check . and python3 -m pytest tests/unit/test_admin_settings.py tests/unit/test_oauth_apps.py -q, then python3 -m mypy src/privacyfence/admin_settings.py src/privacyfence/oauth_apps.py.
      Stop condition: if importing admin_settings from oauth_apps (or the reverse) creates a cycle that a function-level import does not break, stop with status=blocked.
    acceptance:
      - 'python3 -m pytest tests/unit/test_admin_settings.py tests/unit/test_oauth_apps.py -q passes'
      - 'python3 -m pytest tests/unit/test_admin_settings.py tests/unit/test_oauth_apps.py -q --cov=privacyfence.admin_settings --cov=privacyfence.oauth_apps --cov-branch --cov-report=term-missing reports 100% for both modules'
      - 'python3 -m mypy src/privacyfence/admin_settings.py src/privacyfence/oauth_apps.py passes'
  - id: p3-wire-effective-config
    title: Every connector reader sees the effective org config, so a desktop install with no bundle offers the built-in apps
    depends_on: [p2-admin-settings-and-resolution]
    complexity: M
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/server.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_routes_connect.py
    brief: |
      Read "Design → D3" ("Where it is applied").
      1. First, the guard grep: `grep -rnE "(not|if|bool\()\s*org_config\b|len\(org_config\)" src/privacyfence`. The planner found only `org_config = org_config or {}` (daemon_main.py, _maybe_start_web_server) and `org_config or {}` (routes_settings.py:1643), which are harmless. If any other hit decides something from org_config being empty/non-empty, stop with status=blocked naming it.
      2. daemon_main.py: add effective_org_config(bundle) per D3 (AdminSettingsError -> org_mode.ConfigurationError). In run_app rename the load to `bundle = load_org_config()`, call reject_removed_tool_annotations(bundle), then `org_config = effective_org_config(bundle)`; pass `bundle` to log_org_config_bundle_hash; everything else keeps `org_config`. At the CLI dispatch (`org_config = load_org_config()` near line 2518) use `effective_org_config(load_org_config())`.
      3. Thread the raw bundle to org mode: give _start_org_web_server a keyword `bundle: dict[str, Any]`, pass it from run_app, and add a field `bundle: dict[str, Any]` (default factory dict) to web/server.py's OrgAuth dataclass, set where OrgAuth is built. Do not use it yet (p4 does).
      4. settings_controller.py: _org_config_or_empty (line ~1203) and refresh_connectors' work (line ~1240) call `effective_org_config(load_org_config())` (import both from .daemon_main inside the function, as the existing code does). Keep the existing ConfigurationError handling.
      5. Tests. test_daemon_main.py: a class TestEffectiveOrgConfig (no bundle + a monkeypatched builtin slack app -> slack section present; malformed admin_settings.json -> ConfigurationError; bundle passed to log_org_config_bundle_hash is the raw one — monkeypatch it and assert the argument has no synthesized sections). Extend TestBuildConnectorsSlack (or add a new test next to it) so build_connectors(config, effective_org_config({})) with a builtin slack app and a token file builds Slack. test_settings_controller.py: with no bundle and a builtin miro app (monkeypatch app_credentials.builtin_app via the oauth_apps module), the snapshot lists Miro and authenticate_connector("miro") no longer sets "Miro isn't enabled in your organization config."; with admin_settings withdrawing miro, it does. Update existing tests that assumed "no bundle -> no connectors" only where they now fail because a builtin app is visible: they must monkeypatch builtin_app to return None (add an autouse fixture in those test modules, NOT in tests/conftest.py), never weaken their assertions.
      6. Run ruff check . and python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/web/test_server.py tests/unit/web/test_routes_connect.py -q.
      Stop condition: if more than 15 existing tests fail for the "no bundle now offers builtins" reason, stop with status=blocked and list them rather than patching them one by one — the fixture approach in step 5 is supposed to cover them.
    acceptance:
      - 'python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/web/test_server.py tests/unit/web/test_routes_connect.py -q passes'
      - 'python3 -m pytest tests/unit/test_daemon_main.py -q -k TestEffectiveOrgConfig passes and selects at least 3 tests'
      - 'ruff check . passes'
  - id: p4-org-relay-client
    title: Org servers sign in through the relay when a service uses a built-in app
    depends_on: [p3-wire-effective-config]
    complexity: M
    touches:
      - src/privacyfence/relay_client.py
      - tests/unit/test_relay_client.py
      - src/privacyfence/web/routes_connect.py
      - tests/unit/web/test_routes_connect.py
      - src/privacyfence/web/server.py
      - tests/unit/web/test_server_org_mode.py
      - src/privacyfence/daemon_main.py
      - tests/conftest.py
    brief: |
      Read "Design → D5" (the contract and "Org server side"). The Worker is p6; here everything talking to it is mocked.
      1. Guard grep: `grep -rnE "dict\(org_config\)|\{\*\*org_config|copy\(\) *# *org|deepcopy\(org_config" src/privacyfence/daemon_main.py src/privacyfence/web`. If a copy of the org config is handed to routes_connect, build_org_routes, build_connectors or a long-lived object, stop with status=blocked naming it (refresh_in_place relies on one shared dict).
      2. Create src/privacyfence/relay_client.py per D5: well_known_token, register (requests.post exactly as specified), status, reset_for_tests, start_registration_loop. Module state in a small dataclass guarded by a threading.Lock. Never log the token.
      3. tests/conftest.py: call relay_client.reset_for_tests() inside _reset(), next to the other module resets.
      4. web/routes_connect.py: add `redirect_uri: str` to the pending-attempt record (`_PendingAuthStore.put` gains a `redirect_uri` keyword); add _section_for; in start() choose redirect_uri/state per D5 when oauth_apps.uses_relay(_section_for(service, org_config)); in callback() use pending.redirect_uri for the exchange instead of rebuilding it; strip RELAY_MARKER from the Google section before google_oauth.web_client_config, per D5.
      5. web/server.py: in the org-mode route assembly (next to routes_connect.build_routes) add Route("/.well-known/privacyfence-relay", ...) per D5. If server.py or its tests keep a list/classification of unauthenticated org-mode paths, add this path to it with the reason "domain proof for the OAuth relay; returns a random token that grants nothing (ADR 0242)".
      6. daemon_main.py _start_org_web_server: after the web server is built, call relay_client.start_registration_loop(server_config.issuer_url, org_config, <the existing shutdown event used by other background threads in this module>).
      7. Tests. tests/unit/test_relay_client.py: token created once, 43 chars, mode 0600, regenerated when corrupt; register posts the exact JSON with timeout=10 and allow_redirects=False (monkeypatch requests.post); 200 -> ok; 403 with {"error":"proof_failed"} -> detail "proof_failed"; other status -> "HTTP 500"; RequestException -> "could not reach oauth.privacyfence.eu"; the token never appears in caplog; the loop's single iteration (factor the body into a `_tick(issuer_url, org_config, now)` function and test it directly): not needed -> no call; needed and never registered -> call; registered 2 h ago -> no call; registered 25 h ago -> call; last_error set -> call.
         tests/unit/web/test_routes_connect.py: a TestRelay class — with a section carrying "relay": True, /oauth/start/slack redirects to slack.com with redirect_uri=https://oauth.privacyfence.eu/callback/slack and state starting "<netloc>~"; the callback exchanges with that same redirect_uri; jira uses /callback/atlassian; a bundle section without the marker keeps today's redirect_uri. tests/unit/web/test_server_org_mode.py: GET /.well-known/privacyfence-relay without a session returns 200 text/plain with the token and Cache-Control: no-store.
      8. Run ruff check . and python3 -m pytest tests/unit/test_relay_client.py tests/unit/web/test_routes_connect.py tests/unit/web/test_server_org_mode.py tests/unit/web/test_server.py -q.
    acceptance:
      - 'python3 -m pytest tests/unit/test_relay_client.py tests/unit/web/test_routes_connect.py tests/unit/web/test_server_org_mode.py tests/unit/web/test_server.py -q passes'
      - 'python3 -m pytest tests/unit/web/test_routes_connect.py -q -k TestRelay passes and selects at least 4 tests'
      - 'python3 -m pytest tests/unit/test_relay_client.py -q --cov=privacyfence.relay_client --cov-branch --cov-report=term-missing reports 100%'
      - 'ruff check . passes'
  - id: p5-settings-apps-section
    title: Settings → Apps lets an admin offer or withdraw services and replace a built-in app
    depends_on: [p4-org-relay-client]
    complexity: M
    touches:
      - src/privacyfence/web/org_settings_scope.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/connector_registry.py
      - tests/unit/web/test_org_settings_scope.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/test_connector_registry.py
      - src/privacyfence/web/server.py
    brief: |
      Read "Design → D4" in full; action names, parameters, sensitivity, row fields, capability name and every UI string are there. Pattern to copy for an admin_only action in both modes: toggle_pii_detection (org_settings_scope.py ACTION_SCOPES, routes_settings._SENSITIVE_ACTIONS, _ORG_ALLOWED_ACTIONS, _apply_org_action, settings_window_html toggleHtml), added for org mode in commit 8adeb5e3.
      1. connector_registry.py: add evict_all() per D4, with a test in tests/unit/test_connector_registry.py.
      2. org_settings_scope.py: add the four actions with ActionScope(frozenset({LOCAL_MODE, ORG_MODE}), admin_only=True).
      3. routes_settings.py: put offer_service, set_service_app, clear_service_app in _SENSITIVE_ACTIONS and withdraw_service in _NON_SENSITIVE_ACTIONS; add all four to _ORG_ALLOWED_ACTIONS; add build_org_routes keyword `bundle: dict[str, Any]` (pass org.bundle from web/server.py's build_org_routes call); add the _apply_org_action branch per D4 (apply_change, refresh_in_place on the org_config dict build_org_routes already receives, connector_registry.evict_all() when connector_registry is not None, summary + " (admin=<id>)"); map AdminSettingsError to the existing 400 error shape; add "services" and "relay_status" to _org_state per D4; add `"apps"` to _capabilities_for per D4.
      4. settings_controller.py: the four methods per D4; snapshot() gains "services": admin_settings.service_rows(load_org_config() raw bundle or {} on ConfigurationError, admin_settings.load() or the default on AdminSettingsError, mode="local").
      5. settings_window_html.py: the Apps nav section and renderApps per D4, using the existing toggleHtml/dataAttr helpers and the existing form/post pattern used by other text inputs in this file (find the closest existing text-input form, e.g. the Grist API key form, and copy its structure). Secret inputs are type="password" autocomplete="off" and are cleared after a successful post.
      6. Tests: test_org_settings_scope.py — the four actions are admin_only in both modes (extend the existing parametrization, do not add special cases); test_routes_settings.py — TestSensitiveActionsCoverAllAllowedActions still passes; local set_service_app requires step-up when step-up is required (copy an existing TestSensitiveActionStepUp case); the snapshot never contains the posted secret (assert the secret string is absent from the whole response body); test_routes_org_settings.py — a non-admin gets 403 for each action; an admin's set_service_app changes the shared org_config dict's section in place, calls evict_all, and writes an audit entry whose summary contains the client id and not the secret; withdraw_service removes the section; _org_state for a non-admin has services == []; test_settings_controller.py — each method's effect and error path; test_settings_window_html.py — Apps section present when caps.apps, absent otherwise, every status string from D4 rendered for its row source.
      7. Run ruff check . and python3 -m pytest tests/unit/web/test_org_settings_scope.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/test_settings_controller.py tests/unit/test_settings_window_html.py tests/unit/test_connector_registry.py -q.
      Stop condition: if tests/unit/web/test_org_settings_scope.py forbids a new action from being in both modes as admin_only for a reason not covered here, stop with status=blocked.
    acceptance:
      - 'python3 -m pytest tests/unit/web/test_org_settings_scope.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/test_settings_controller.py tests/unit/test_settings_window_html.py tests/unit/test_connector_registry.py -q passes'
      - 'grep -n "\"set_service_app\"" src/privacyfence/web/org_settings_scope.py src/privacyfence/web/routes_settings.py matches in both files'
      - 'ruff check . passes'
  - id: p6-relay-worker
    title: The OAuth redirect relay Worker at oauth.privacyfence.eu and its deploy workflow
    depends_on: []
    complexity: M
    touches:
      - cloudflare/oauth-relay/**
      - .github/workflows/deploy-oauth-relay-worker.yml
      - .github/workflows/dependency-audit.yml
      - .claude/toolkit.yaml
    brief: |
      Read "Design → D5" (the contract and "The Worker") in full; routes, codes, regexes, texts and headers are there.
      1. Create cloudflare/oauth-relay/ by copying cloudflare/downloads' package.json (rename to privacyfence-oauth-relay; same scripts, devDependencies and overrides), tsconfig.json, vitest.config.ts (drop readD1Migrations/TEST_MIGRATIONS; keep the coverage thresholds), and write wrangler.toml per D5 (Durable Object REGISTRY, class Registry, migration tag v1 with new_sqlite_classes). Run `npm install` there to produce package-lock.json with the same versions as cloudflare/downloads/package-lock.json.
      2. src/index.ts (routing, headers), src/registry.ts (the Durable Object class Registry extending DurableObject from "cloudflare:workers", creating the table in its constructor with ctx.storage.sql, with methods getRegistration(host), upsertVerified(host, nowIso), listActive()), src/validate.ts (host/token/grant/state parsing and the non-public-host check). worker-configuration.d.ts: `interface Env { REGISTRY: DurableObjectNamespace<import("./src/registry").Registry>; }`.
      3. Implement POST /admin/revoke per D5 (REVOKE_TOKEN: string in Env; constant-time comparison by comparing SHA-256 digests of both values with crypto.subtle.timingSafeEqual). Write cloudflare/oauth-relay/README.md: what the relay does, the contract from D5, how a maintainer revokes a host (the curl call to /admin/revoke with the bearer token), and that the token comes from the GitHub environment secret CF_OAUTH_RELAY_REVOKE_TOKEN.
      4. Tests (test/*.test.ts with the workers pool): health; register happy path (mock the outbound fetch with fetchMock from "cloudflare:test" or the pool's outbound mock, whichever cloudflare/downloads' tests already use — if they use none, use `fetchMock` from "cloudflare:test"); invalid host/token -> 400 invalid_request; localhost / 10.0.0.1 / x.internal -> 400 host_not_public; proof mismatch / 404 / redirect -> 403 proof_failed; revoked -> 403 revoked; callback forwards with the exact original query to https://<host>/oauth/callback/<grant>; unknown grant -> 404; unregistered / revoked / verified 36 days ago -> 400 with the exact text; /registrations lists non-revoked sorted; every response has the three headers; /admin/revoke 401 without/with wrong token, 200 with the right one.
      5. .github/workflows/deploy-oauth-relay-worker.yml per D5 (copy deploy-download-worker.yml; job names verify and deploy; no D1 step).
      6. .github/workflows/dependency-audit.yml: add a job npm-audit-oauth-relay copied from npm-audit-worker with working-directory cloudflare/oauth-relay, and add cloudflare/oauth-relay/package.json and package-lock.json to the same paths filters the worker's files are in.
      7. .claude/toolkit.yaml verify.dod_conditional: add an entry `when: ["cloudflare/oauth-relay/**"]`, `do: "In cloudflare/oauth-relay/: \`npm test\`, \`npm run typecheck\` and \`npm run dry-run\` must pass."`.
      8. Run in cloudflare/oauth-relay: npm ci, npm run typecheck, npm test, npm run dry-run.
      Stop condition: if the vitest workers pool version pinned in cloudflare/downloads cannot run a SQLite Durable Object (an error naming new_sqlite_classes or ctx.storage.sql), stop with status=blocked; do not switch to D1 (D1 needs a dashboard-provisioned id this plan does not have).
    acceptance:
      - 'cd cloudflare/oauth-relay && npm ci && npm run typecheck && npm test && npm run dry-run passes'
      - 'grep -n "oauth.privacyfence.eu" cloudflare/oauth-relay/wrangler.toml matches'
      - 'grep -n "enabled = false" cloudflare/oauth-relay/wrangler.toml matches'
      - 'grep -n "cloudflare/oauth-relay" .claude/toolkit.yaml .github/workflows/dependency-audit.yml matches in both'
  - id: p7-docs
    title: Reference docs, privacy page and changelog for built-in apps, Settings → Apps and the relay
    depends_on: [p5-settings-apps-section, p6-relay-worker]
    complexity: M
    touches:
      - docs/getting-started.md
      - docs/connecting-a-service.md
      - docs/org-mode-setup-guide.md
      - docs/configuration-reference.md
      - docs/google-cloud-setup.md
      - docs/slack-setup.md
      - docs/salesforce-setup.md
      - docs/atlassian-setup.md
      - docs/miro-setup.md
      - docs/telegram-setup.md
      - docs/security-and-compliance.md
      - website/privacy/index.html
      - CHANGELOG.md
      - tests/unit/test_website_claims.py
    brief: |
      Reference docs describe today's behaviour and link ADRs 0239-0242 for the why (the ADR files are written in p8; link them by their final file names: 0239-built-in-oauth-apps-ship-in-every-distribution.md, 0240-an-apps-credentials-resolve-bundle-then-admin-then-built-in.md, 0241-admin-settings-live-outside-the-signed-bundle.md, 0242-org-servers-use-built-in-apps-through-a-domain-verified-relay.md).
      1. getting-started.md "Connect your services" and connecting-a-service.md: a fresh install offers Google, Slack, Salesforce, Atlassian, Miro and Telegram with built-in apps and no bundle; Settings → Apps (who may use it, step-up, the status strings from Design D4); a bundle section still wins and shows locked. Keep the bundle-install section, now described as optional.
      2. Each provider setup doc (google-cloud, slack, salesforce, atlassian, miro): a short opening "You need this page only to use your organization's own app instead of the built-in one" paragraph, how to enter it in Settings → Apps as well as by bundle, and the provider caveat: Google unverified-app screen and 100-user cap until verification, Workspace admins can trust the client ID; Salesforce orgs that block uninstalled connected apps need an admin to install or approve it; Slack workspaces that require app approval need an admin's approval once; Atlassian and Miro need nothing extra. In google-cloud-setup.md say an org-mode override must be a Web application client. telegram-setup.md: the Settings → Apps switch.
      3. org-mode-setup-guide.md §5 "Connector apps": built-in apps through the relay are the default; what the relay is, the domain proof (the server must be reachable from the internet at https://<server>/.well-known/privacyfence-relay; with §7's IP restriction, allow that path or use your own apps), the 35-day re-verification, the public /registrations list, how to read registration status in Settings → Apps; and §15 Troubleshooting rows for "Relay registration failed: proof_failed", "host_not_public", "revoked", and the relay's 400 text from Design D5.
      4. configuration-reference.md: org/admin_settings.json (schema from D2, written only by Settings, not to be hand-edited) and org/relay_token.txt; the PRIVACYFENCE_BUILTIN_APPS development variable.
      5. security-and-compliance.md: a short "Built-in apps and the relay" subsection: built-in secrets are public in effect (ADR 0239), what the relay sees (the authorization code in transit, never stored or logged) and does not (no tokens), and the domain-proof trust model (ADR 0242).
      6. website/privacy/index.html: extend the scope sentence (lines ~26-27) to name oauth.privacyfence.eu; add a section "Sign-in relay (oauth.privacyfence.eu)" after the download section: a small Cloudflare Workers service that forwards an organization server's sign-in redirect; it processes the IP address and request in transit, stores only each registered server's host name and registration dates (shown publicly at /registrations), keeps no request logs, and is used only by organization servers that use the built-in apps; legal basis Art. 6(1)(f) as for hosting. Adjust "sends nothing to the project" (lines ~45-54) to say the only exception is an organization server's registration of its host name with the relay. Update "Last updated" to the date you make the change. If tests/unit/test_website_claims.py pins any sentence you change, update that test's expected text to the new sentence (and nothing else).
      7. CHANGELOG.md under "## [Unreleased]": one entry each for built-in apps (desktop), Settings → Apps, and organization servers using built-in apps through the relay. No version heading.
      8. Run python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_website_docs_pages.py -q and ruff check ..
    acceptance:
      - 'python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_website_docs_pages.py -q passes'
      - 'grep -n "oauth.privacyfence.eu" website/privacy/index.html docs/org-mode-setup-guide.md docs/security-and-compliance.md matches in each file'
      - 'grep -n "Settings → Apps" docs/connecting-a-service.md docs/slack-setup.md matches in both'
      - 'git diff origin/main -- CHANGELOG.md | grep -c "^+## \[" prints 0, and the added entry lines sit below the "## [Unreleased]" heading'
  - id: p8-retire-plan
    title: ADRs 0239-0242, status lines on 0040 and 0196, and delete the plan
    depends_on: [p7-docs]
    complexity: S
    touches:
      - docs/adr/0239-built-in-oauth-apps-ship-in-every-distribution.md
      - docs/adr/0240-an-apps-credentials-resolve-bundle-then-admin-then-built-in.md
      - docs/adr/0241-admin-settings-live-outside-the-signed-bundle.md
      - docs/adr/0242-org-servers-use-built-in-apps-through-a-domain-verified-relay.md
      - docs/adr/README.md
      - docs/adr/0040-telegram-app-credentials-ship-in-every-distribution.md
      - docs/adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md
      - docs/builtin-oauth-apps-plan.md
      - docs/builtin-oauth-apps-plan-manual-steps.html
    brief: |
      1. Before writing, check `ls docs/adr | tail -3`: if 0239-0242 are taken by ADRs merged to main since this plan, take the next free numbers, and update every link to them in docs/ (p7 linked the planned names) in this same phase.
      2. Write the four ADRs in docs/adr/README.md's template (Status Accepted with today's date and "Implemented", Context, Decision, Alternatives considered, Consequences, Verification, Related). Their decisions are the "ADRs" section of this plan; their context, alternatives and consequences come from this plan's Goal, Design D1-D5 and "Accepted risks". Alternatives to record: 0239 — fetch credentials from a PrivacyFence endpoint at runtime, have the relay broker the token exchange and hold the secrets; 0240 — keep requiring a bundle section (ADR 0196 as is), let the admin settings override a bundle section; 0241 — let the UI rewrite org_config.json (cannot be re-signed), keep overrides in settings.yaml (per-principal, not install-wide), let any local account change them; 0242 — no relay (org servers register their own apps, as before), a maintainer-approved list of servers, forwarding to any server that asks. Link source files, never the plan. Do not mention any model or agent.
      3. ADR 0040 and ADR 0196: add one line each to their Status section only: "Amended by [0239](...)" and "Amended by [0240](...)".
      4. docs/adr/README.md: four index rows in the existing format ("Accepted; relates to 0040" / "Accepted; amends 0196" / "Accepted; relates to 0016, 0034, 0070" / "Accepted; relates to 0011, 0186").
      5. Delete docs/builtin-oauth-apps-plan.md and docs/builtin-oauth-apps-plan-manual-steps.html; then `grep -rn "builtin-oauth-apps-plan" .` (excluding .git) must find nothing.
    acceptance:
      - 'ls docs/adr/0239-*.md docs/adr/0240-*.md docs/adr/0241-*.md docs/adr/0242-*.md lists four files (or the renumbered four)'
      - 'grep -c "^## Status" docs/adr/0239-*.md docs/adr/0240-*.md docs/adr/0241-*.md docs/adr/0242-*.md reports 1 for each'
      - 'test ! -e docs/builtin-oauth-apps-plan.md && test ! -e docs/builtin-oauth-apps-plan-manual-steps.html'
      - 'grep -rn "builtin-oauth-apps-plan" --exclude-dir=.git . finds nothing'
```
