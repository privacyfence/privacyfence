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
  wins and shows as locked in Settings. A bundle that lists connectors keeps that curated list on
  upgrade: the services it leaves out stay off until an admin switches them on (D2).

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
  `MODULE_SUFFIX = "privacyfence/_builtin_apps.py"`. Its messages read as the Telegram ones do, with
  "Telegram" replaced by "built-in OAuth apps" and the ADR number changed to 0239. For example, the
  error line ends `...; built-in OAuth apps will not work on this install. A stable release must
  ship it (ADR 0239).`

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
- When the file, the service or the `offered` key is missing, the default is decided by the bundle,
  through `admin_settings.default_offered(bundle) -> bool`:
  - It returns `not any(bundle.get(s) for s in set(org_mode.CONNECTOR_ORG_SECTION.values()))`.
  - A fresh install, or a bundle with no connector sections at all, offers everything it has an app
    for.
  - An existing bundle that lists connector sections keeps its curated list after the upgrade (ADR
    0196 semantics), in local and org mode. So an org server whose bundle left Slack out does not
    start offering Slack, or contacting the relay, until an admin switches it on.
- The path is `admin_settings.admin_settings_path() -> Path`, which returns
  `paths.org_dir() / "admin_settings.json"` at call time. Tests patch this function.
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
- `offered(data, service, *, bundle: dict[str, Any]) -> bool`: the stored value, else
  `default_offered(bundle)`.
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
    2. **Not `admin_settings.offered(admin, service, bundle=bundle)`**: no section (source
       `none`).
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
  - The CLI dispatch at `:2518` uses
    `oauth_apps.effective_org_config(bundle, admin_settings.load(), mode="local")`, with
    `bundle = load_org_config()`. The CLI OAuth runners are loopback flows, so they always use the
    local-mode apps, even on an org-mode host.
  - `settings_controller._org_config_or_empty` (`:1203`) and `refresh_connectors`'s `work`
    (`:1240`) call `effective_org_config(load_org_config())`.
  - The QA scripts (`scripts/qa_*.py`) are not changed. They run against the runner's own bundle.
- In org mode the raw `bundle` travels down through a new keyword: `run_app` →
  `_maybe_start_web_server(..., bundle=bundle)` (new keyword `bundle: dict[str, Any] | None =
  None`; `run_app` calls it at `daemon_main.py:2160`) → `_start_org_web_server(..., bundle=...)`
  (called at `:801-805`). That function stores it on `OrgAuth` as a new field `bundle:
  dict[str, Any]`. `build_org_routes` gets a new keyword
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
| `set_service_app` | `service: str, client_id: str, client_secret: str, login_url: str = ""` | yes |
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
  carries no payload. This differs on purpose from org mode's descriptive summary: local mode's
  audit path is shared by every action and is left as it is.

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
- An `AdminSettingsError` becomes the error the existing branches already return:
  `except admin_settings.AdminSettingsError as exc: raise
  org_install_policy.PolicyChangeRejected(str(exc)) from exc`. It is caught at
  `routes_settings.py:2110`, like the branches at `:2015` and `:2068`.
- An `OSError` from `admin_settings.save` is caught in the branch and logged as `Could not write
  the admin settings file`. It is re-raised the way the settings.yaml write's `OSError` is.

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

**Capabilities.** In `settings_window_html.py`:

- Add `"apps"` to `_ALL_SECTIONS` (`:2502`).
- In `_capabilities_for` (`:2525`), set `sections["apps"] = is_admin` in org mode. Local mode gets
  `True` from the existing `dict.fromkeys`.
- The JS reads `CAPS.sections.apps`.
- The tests that pin the section sets are at `tests/unit/test_settings_window_html.py:908-959`.

**Switching apps signs people out of that service.** Salesforce, Atlassian and Miro refresh with
the current app's credentials, and a token from one app does not refresh under another. Slack and
Google do not refresh this way, but the next sign-in uses the new app.

- The plan accepts this; there is no migration.
- The UI says it before the change: the "Use your own app" form and the "Use the built-in app"
  button each carry the line `Everyone who already connected <Label> will need to connect again.`
- The docs (p7a) say it too.

**UI: `settings_window_html.py`.** A new nav section **Apps**, shown when `CAPS.sections.apps`,
rendered by `renderApps(state)`.

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

- The relay serves only an `issuer_url` of the form `https://host[:port]` with an empty path or
  `/`. Any other issuer_url (http, or a path prefix) cannot use the relay.
- `host` is `urlsplit(issuer_url).netloc.lower()`, for example `pf.acme.example.com` or
  `pf.acme.example.com:8443`.
- A host is valid when **all** of these hold:
  - its total length is at most 253;
  - split at the last `:`, the port part, if any, is a decimal number from 1 to 65535 with no
    leading zero;
  - the name has at least two dot-separated labels;
  - every label matches `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`;
  - the last label matches `^[a-z]{2,63}$`.

  The last-label rule rejects every IPv4 form (`127.1`, `2130706433`, `0x7f000001`) and a trailing
  dot. Then:
  - the names `localhost` and those ending in `.localhost`, `.local`, `.internal`, `.home.arpa` or
    `.lan` are "not public";
  - in the Worker, `new URL("https://" + host).host === host` must also hold.

  Python implements it as `relay_client.valid_relay_host(host) -> bool` and
  `relay_client.public_host(host) -> bool`; the Worker implements it in `src/validate.ts`. Both
  sides test the same cases: the examples above, `a..b`, `-x.com`, `x.com:0`, `x.com:99999`,
  `foo.localhost.`, and `pf.acme.example.com:8443` as valid.
- The well-known token is 43 characters of `[A-Za-z0-9_-]` (`secrets.token_urlsafe(32)`).
- `state` for a relayed sign-in is `f"{host}~{secrets.token_urlsafe(32)}"`.

**Org server side: new module `src/privacyfence/relay_client.py`.**

- `well_known_token() -> str` reads `paths.org_dir() / "relay_token.txt"`, or creates it with
  `atomic_write_text` (mode 0600) when it is missing or not 43 valid characters.
- `register(issuer_url: str) -> tuple[bool, str]` first checks the issuer_url and host rules
  above. On failure it returns, without calling the Worker:
  - `(False, "the relay needs an https issuer_url with no path")`, or
  - `(False, "host_not_public")`.

  Otherwise it does
  `requests.post(f"{RELAY_BASE_URL}/register", json={"host": host, "token": well_known_token()}, timeout=10, allow_redirects=False)`.
  - 200 gives `(True, "")`.
  - Otherwise `(False, detail)`, where `detail` is the response JSON's `error` string when it is one
    of the Worker's codes below, `HTTP <status>` otherwise, or `could not reach
    oauth.privacyfence.eu` on `requests.RequestException`.
- `status() -> dict[str, Any]` returns `{"last_ok_at": float | None, "last_error": str,
  "needed": bool}` from module state. It is reset by a new `relay_client.reset_for_tests()` called
  from `tests/conftest.py::_reset`.
- `request_check() -> None` sets a module-level `threading.Event` (`_wake`) that the loop waits on.
- `_tick(issuer_url: str, org_config: dict[str, Any], now: float) -> float` runs one check and
  returns the number of seconds to wait before the next:
  - `needed` = any section in `org_config` with `oauth_apps.uses_relay(section)`.
  - Not needed: no call, wait 3600.
  - Needed and `last_ok_at` within 86400 s and no `last_error`: no call, wait 3600.
  - Otherwise it calls `register` and updates the state.
    - On success: clear `last_error`, reset the backoff, wait 3600.
    - On failure: set `last_error`, wait the backoff. The backoff is 60 s, doubling on each
      consecutive failure up to 3600 s.
- `start_registration_loop(issuer_url: str, org_config: dict[str, Any], stop: threading.Event) -> threading.Thread`
  starts a daemon thread that loops until `stop` is set:
  - it calls `_tick`;
  - it waits on `_wake` for at most the returned seconds, then clears it.
  - It logs at INFO on success and at WARNING with `detail` on failure, never the token.
- Where it starts: in `daemon_main._start_org_web_server`, right after `server.start()`
  (`daemon_main.py:1249`), with the shared `org_config` and `daemon_main._shutdown_event`
  (`:268`). It is followed by `relay_client.request_check()` once the server reports ready. If
  `server.start()` returns only after the server is ready, the call goes directly after it.
- p5a's org admin branch calls `relay_client.request_check()` after every successful change.
- `/oauth/start` for a relayed service whose `status()["last_ok_at"]` is None redirects to
  `/connect?error=<service>`, the existing error path, and logs `relay registration has not
  succeeded yet`. It never sends the person to the provider for a sign-in the relay would refuse.

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
- The section for a service is `org_config.get(_ORG_CONFIG_SECTION[service]) or {}`, using the
  existing `routes_connect._ORG_CONFIG_SECTION` (`:114-115`). There is no new helper.
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
  `registrations(host TEXT PRIMARY KEY, registered_at TEXT NOT NULL DEFAULT '', verified_at TEXT NOT NULL DEFAULT '', last_attempt_at TEXT NOT NULL DEFAULT '', revoked INTEGER NOT NULL DEFAULT 0)` (an empty `verified_at` means never verified; `/registrations` and `/callback` treat it as unregistered),
  accessed through the single instance `env.REGISTRY.idFromName("global")`.
- Routes (anything else returns 404):
  - `GET` and `HEAD /health` → `200`. The body is `ok`, and HEAD has no body, as in
    `cloudflare/downloads/src/index.ts:223-224`.
  - `POST /register`, JSON body `{host, token}`:
    - Validate `host` with the host rules above and `token` against `^[A-Za-z0-9_-]{43}$`. Failure
      gives 400 `{"error":"invalid_request"}`.
    - A host that is not public gives 400 `{"error":"host_not_public"}`.
    - A revoked host gives 403 `{"error":"revoked"}`.
    - A host whose previous `/register` attempt (successful or not) was less than 60 s ago gives
      429 `{"error":"too_soon"}`. The attempt time is a `last_attempt_at` column, updated before
      the proof fetch; a host with no row gets one inserted with `verified_at` empty.
    - Fetch `https://<host>/.well-known/privacyfence-relay` with `redirect: "manual"`, a 5 s
      timeout (`AbortSignal.timeout(5000)`) and the body read up to 256 bytes. A non-200 response,
      or a trimmed body different from `token`, gives 403 `{"error":"proof_failed"}`.
    - On success, upsert `registered_at` (kept from the first registration) and `verified_at =
      now`, then return 200 `{"host", "verified_at"}`.
  - `GET /callback/:grant`, where `grant` is in `gmail`, `drive`, `calendar`, `contacts`, `tasks`,
    `apps_script`, `slack`, `salesforce`, `atlassian` or `miro`:
    - Read `state`, split at the first `~`, and validate the host part with the host rules.
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
  - smoke test `curl --head https://oauth.privacyfence.eu/health`;
  - job names `verify-oauth-relay` and `deploy-oauth-relay` (not `verify`, which the download Worker's workflow already uses).

**Accepted risks**, recorded in ADR 0242:

- Slack documents no PKCE, so a relayed Slack code passes through the Worker in the browser
  redirect. The Worker keeps no logs and the code is single-use.
- Anyone who owns a public domain can register a server and run sign-ins under the PrivacyFence
  name. Domain proof makes this cost a domain and a running server, and revocation removes a
  reported host.
- An org server not reachable from the internet, for example one restricted by IP address (setup
  guide §7), cannot register. It must use its own apps.

### D6. Test isolation

In a source checkout `paths.data_dir()` is the repository root, so a test that forgets to patch it
would write `org/admin_settings.json` or `org/relay_token.txt` into the checkout. A developer with a
generated `_builtin_apps.py` or `PRIVACYFENCE_BUILTIN_APPS` set would also see unrelated tests
change.

p2 adds an autouse fixture `_isolate_builtin_apps_and_admin_settings` to `tests/conftest.py`, in
the style of the existing `_isolate_whats_new_state` fixture:

- It patches `admin_settings.admin_settings_path` to `tmp_path / "admin_settings.json"`.
- It patches `app_credentials.builtin_app` (and the name `oauth_apps` imports) to return `None`.

p4 extends the same fixture to patch `relay_client.relay_token_path` (a function returning
`paths.org_dir() / "relay_token.txt"`) to `tmp_path / "relay_token.txt"`. Tests that need built-in
apps patch `builtin_app` themselves.

## ADRs

- **0239** — Built-in PrivacyFence OAuth apps for Google, Slack, Atlassian, Salesforce and Miro
  ship in every distribution, baked in from CI secrets by `scripts/builtin_apps.py`. Relates to 0040:
  same mechanism, new apps; 0040 is not amended and gets no status line.
- **0240** — An app's credentials resolve as bundle section → admin settings → built-in app.
  Which services are offered is an admin switch in Settings for every service the bundle has no
  section for. It is on by default only when the bundle has no connector section at all, so a
  curated bundle keeps its list on upgrade. Amends 0196.
- **0241** — Admin settings live in a daemon-written `org/admin_settings.json` outside the signed
  bundle. They are changed only by the install owner (local) or an IdP admin (org), with step-up,
  and a client secret is never sent back to a browser. The file has settings.yaml-level trust (the
  install directory), not the signed bundle's: an organization that wants an app pinned puts it in
  a bundle section. Relates 0016, 0034, 0070, 0200.
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
- **Provider constraints the code cannot enforce.** These are documented in p7a, not built:
  - Google's unverified-app screen and its 100-user cap until verification. Gmail and Drive scopes
    are restricted, so verification includes an annual third-party security assessment (CASA).
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
    why: p1 bakes them into every build (and its dispatched build.yml check looks for them); ma1 and ma2 sign in with them
    done_when: Settings → Secrets and variables → Actions lists the 14 repository secrets PF_APP_GOOGLE_DESKTOP_CLIENT_ID/_SECRET, PF_APP_GOOGLE_WEB_CLIENT_ID/_SECRET, PF_APP_SLACK_CLIENT_ID/_SECRET, PF_APP_SALESFORCE_CLIENT_ID/_SECRET, PF_APP_ATLASSIAN_LOCAL_CLIENT_ID/_SECRET, PF_APP_ATLASSIAN_RELAY_CLIENT_ID/_SECRET, PF_APP_MIRO_CLIENT_ID/_SECRET
  - id: mb2-relay-worker-cloudflare
    title: Create the Cloudflare API token and the oauth-relay-worker GitHub environment for the relay Worker
    why: the relay's deploy workflow (p6) uses them once the PR reaches main, and ma2 deploys from the branch by hand with the same token
    done_when: the repository has an environment named oauth-relay-worker, restricted to main, holding secrets CF_OAUTH_RELAY_WORKER_API_TOKEN, CF_OAUTH_RELAY_WORKER_ACCOUNT_ID and CF_OAUTH_RELAY_REVOKE_TOKEN, and the API token verifies with the curl in the page
  - id: mb3-google-verification
    title: (Optional, long-running) Submit the Google consent screen for verification
    why: until verified, Google shows an unverified-app screen and caps the app at 100 users; nothing in the plan waits for it
    done_when: the Google Cloud console's Verification Center shows the submission, or the user chose to defer it
manual_after:
  - id: ma1-desktop-builtin-signin
    title: Install a desktop build of the feature branch (from a dispatched build.yml run) with no organization bundle, and sign in to Gmail, Slack, Salesforce, Jira and Miro with the built-in apps
    why: real provider consent screens and loopback redirects cannot run in CI
  - id: ma2-org-relay-signin
    title: Deploy the relay from the feature branch with wrangler, then on a test org server with no connector sections in its bundle, register with the relay and connect Gmail, Slack, Salesforce, Jira and Miro through it
    why: the relay round trip needs a public domain, the real Worker and real providers; the deploy workflow only deploys main
  - id: ma3-override-and-withdraw
    title: In Settings → Apps, replace one built-in app with your own and withdraw one service, on both a desktop install and the org server
    why: checks the admin path end to end, with step-up, against a real provider
verify_after_merge:
  - 'python3 -m mypy src/privacyfence'
  - 'python3 scripts/mypy_strict_modules.py'
  - 'test ! -d cloudflare/oauth-relay || (cd cloudflare/oauth-relay && npm ci && npm run typecheck && npm test)'
final_checks:
  - 'docs/builtin-oauth-apps-plan.md and docs/builtin-oauth-apps-plan-manual-steps.html are deleted, and grep -rn "builtin-oauth-apps-plan" --exclude-dir=.git . finds nothing'
  - 'ADRs 0239, 0240, 0241 and 0242 (or their renumbered successors) exist, are Accepted, and are listed in docs/adr/README.md''s index; ADR 0196''s Status section carries one line pointing at 0240; ADR 0040 is unchanged'
  - 'CHANGELOG.md has the new entries under "## [Unreleased]" and no new version heading'
  - 'cd cloudflare/oauth-relay && npm ci && npm test && npm run typecheck && npm run dry-run passes'
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
      1. Create scripts/builtin_apps.py by copying the structure of scripts/telegram_credentials.py (stdlib only, REPO_ROOT, render(), write(creds_path, environ), archive_members/contains_credentials/check_dist, argparse main with `write` and `check-dist [--require] files*`). KEYS = ("google_desktop", "google_web", "slack", "salesforce", "atlassian_local", "atlassian_relay", "miro"); env vars PF_APP_<KEY.upper()>_CLIENT_ID / _CLIENT_SECRET. render(apps) returns exactly "APPS = " + repr(dict(sorted(apps.items()))) + "\n". Messages exactly as D1 says (check-dist's text is telegram_credentials.py:94-99's with "Telegram" replaced by "built-in OAuth apps" and ADR 0040 by ADR 0239); ASCII only.
      2. Add `src/privacyfence/_builtin_apps.py` to .gitignore directly below the `_telegram_credentials.py` line, and `include src/privacyfence/_builtin_apps.py` to MANIFEST.in directly below line 6, extending the comment above it to name both generated modules.
      3. In src/privacyfence/app_credentials.py add BUILTIN_APP_KEYS and builtin_app(key) per D1 (a literal `from . import _builtin_apps` with the same `# type: ignore[attr-defined]` comment the Telegram import carries; the PRIVACYFENCE_BUILTIN_APPS JSON-file fallback; one logger.warning naming only the env var on a bad file). Extend the module docstring by one paragraph naming ADR 0239.
      4. Call the generator next to every Telegram generator call: scripts/build_dmg.sh (after line 109: `"$PYTHON" scripts/builtin_apps.py write`), scripts/build_deb.sh (after line 95), scripts/build_installer.ps1 (after lines 94-95, followed by `if ($LASTEXITCODE -ne 0) { throw "Writing built-in app credentials failed" }`).
      5. .github/workflows/build.yml: add the 14 `PF_APP_*: ${{ secrets.PF_APP_* }}` entries to the step-level `env:` of the `build` job's "Build .app, .pkg, .mcpb and DMG" step, the `build-windows` "Build installer" step and the `build-deb` "Build .deb" step. In `build-org-image`, add a step "Write built-in app credentials" right after "Write Telegram app credentials" (same shape, the 14 env entries, `python3 scripts/builtin_apps.py write`) and a step "Check built-in app credentials are packaged" right after the Telegram check step (same REQUIRE logic, `python3 scripts/builtin_apps.py check-dist "${REQUIRE[@]}" dist/*.whl`).
      6. .github/workflows/publish-pypi.yml `build` job: the same two steps next to the Telegram ones, with check-dist over `dist/*.whl dist/*.tar.gz`. The secrets go only to the write step's env, never to the job.
      7. Tests. tests/unit/test_builtin_apps_script.py mirrors every test in tests/unit/test_telegram_credentials.py: it generates an importable module whose APPS equals the input; a value with quotes and backslashes stays inert (ast.literal_eval of the right-hand side equals the input); one-of-two set warns and skips; none set deletes a stale module; a newline in a value returns 2 and writes nothing; output is ASCII; check-dist pass, require-fail, warn, no files and unknown archive; main dispatch; git-ignored but packaged; build_dmg.sh, build_deb.sh, build_installer.ps1, build.yml and publish-pypi.yml call `scripts/builtin_apps.py write`, build.yml and publish-pypi.yml call `scripts/builtin_apps.py check-dist`, and none contains `APPS = `. tests/unit/test_app_credentials.py: builtin_app returns the generated module's entry (monkeypatch sys.modules["privacyfence._builtin_apps"]), returns a copy, falls back to the JSON file, returns None and warns once for a missing or malformed file without logging its content; and BUILTIN_APP_KEYS order.
      8. Run ruff check . and python3 -m pytest tests/unit/test_builtin_apps_script.py tests/unit/test_app_credentials.py tests/unit/test_telegram_credentials.py tests/unit/test_org_image_workflows.py -q.
      9. After pushing the phase branch, dispatch build.yml on it (it is in ci.dispatchable; mb1's secrets are in place by now). In the run, the "Build .deb" step's log and the build-org-image "Write built-in app credentials" step's log must each show `built-in app google_desktop: included`. If they show `not set`, check the env wiring of step 5; if the secrets themselves are missing, stop with status=blocked naming mb1.
      Stop condition: if `git grep -n "_telegram_credentials" setup.py` shows that setup.py handles the Telegram module, mirror it for _builtin_apps; if the Telegram module reaches the wheel by a mechanism you cannot mirror, stop with status=blocked.
    acceptance:
      - 'python3 -m pytest tests/unit/test_builtin_apps_script.py tests/unit/test_app_credentials.py tests/unit/test_telegram_credentials.py tests/unit/test_org_image_workflows.py -q passes'
      - 'grep -c "scripts/builtin_apps.py write" .github/workflows/build.yml .github/workflows/publish-pypi.yml scripts/build_dmg.sh scripts/build_deb.sh scripts/build_installer.ps1 reports at least 1 for each file'
      - 'grep -n "src/privacyfence/_builtin_apps.py" .gitignore MANIFEST.in matches in both files'
      - 'the dispatched build.yml run''s "Build .deb" log contains "built-in app google_desktop: included"'
      - 'ruff check . passes'
  - id: p2-admin-settings-and-resolution
    title: Admin settings store, the effective org config resolver and test isolation
    depends_on: [p1-builtin-apps-generator]
    complexity: M
    touches:
      - src/privacyfence/admin_settings.py
      - src/privacyfence/oauth_apps.py
      - tests/unit/test_admin_settings.py
      - tests/unit/test_oauth_apps.py
      - tests/conftest.py
      - scripts/check_coverage_floor.py
    brief: |
      Read "Design → D2", "D3" (the oauth_apps part only), the "Snapshot rows" table in D4 and "D6". Every constant, signature, message and rule is there. These are two new pure modules; no caller changes in this phase.
      1. Create src/privacyfence/admin_settings.py with exactly D2's names: AdminSettingsError, MANAGED_SERVICES, SERVICE_LABELS, admin_settings_path, default_offered, load, save, offered, own_app, apply_change and _write_lock. Use secure_files.atomic_write_json. Error texts and summaries are verbatim from D2. Also add service_rows(bundle, admin, *, mode) per D4's row table. It imports oauth_apps inside the function, not at module level, to avoid an import cycle.
      2. Create src/privacyfence/oauth_apps.py with exactly D3's constants and functions: RELAY_BASE_URL, RELAY_MARKER, GOOGLE_AUTH_URI, GOOGLE_TOKEN_URI, BUILTIN_KEY, AppSource, resolve_source, effective_org_config, refresh_in_place, relay_redirect_uri and uses_relay. The per-service rules apply exactly in D3's order 1-6, and rule 2 calls admin_settings.offered(admin, service, bundle=bundle). Import builtin_app and telegram_app_credentials as module attributes (`from . import app_credentials`, then call `app_credentials.builtin_app(...)`), so a single monkeypatch of app_credentials covers it.
      3. tests/conftest.py: add the autouse fixture from D6, patching admin_settings.admin_settings_path and app_credentials.builtin_app. Model it on _isolate_whats_new_state (lazy tmp_path through request.getfixturevalue).
      4. scripts/check_coverage_floor.py: add "src/privacyfence/admin_settings.py" and "src/privacyfence/oauth_apps.py" to MODULE_FLOORS at 100, in the same entry format the file already uses.
      5. tests/unit/test_oauth_apps.py:
         - A parametrized table over bundle section present / not offered / admin app / builtin present / builtin absent, × mode (local, org), × service. It asserts the resulting section and resolve_source.
         - A bundle section is kept verbatim with its "relay" key stripped.
         - Google sections carry auth_uri/token_uri.
         - The Salesforce built-in section uses consumer_key/consumer_secret.
         - Org built-in sections carry "relay": True; local ones do not.
         - Telegram gets {"enabled": True} only when telegram_app_credentials() is not None.
         - The default_offered upgrade rule: a bundle with a slack section and no miro section leaves miro out unless the admin file offers it; an empty bundle offers miro.
         - The inputs are not mutated.
         - refresh_in_place removes missing keys and updates the others on the same object.
         - relay_redirect_uri("slack") == "https://oauth.privacyfence.eu/callback/slack".
      6. tests/unit/test_admin_settings.py:
         - The missing-file default.
         - A malformed file, a non-object, or a wrong version raises AdminSettingsError, and the message does not contain the file's content.
         - Each apply_change action's stored result and returned summary.
         - The Salesforce field mapping and the optional login_url.
         - Every error text in D2.
         - set_service_app for Telegram is refused.
         - The secret appears in no exception message, no log record (caplog) and no returned value.
         - The saved file has mode 0o600 on POSIX; skip this on Windows the way the existing secure_files tests do.
         - service_rows fields for each source; no row has a key containing "secret".
      7. Run ruff check ., then python3 -m pytest tests/unit/test_admin_settings.py tests/unit/test_oauth_apps.py -q, python3 -m pytest tests/unit -q (the new autouse fixture must not break anything), and python3 -m mypy src/privacyfence/admin_settings.py src/privacyfence/oauth_apps.py.
      Stop condition: if the import cycle between admin_settings and oauth_apps is not broken by the function-level import, stop with status=blocked.
    acceptance:
      - 'python3 -m pytest tests/unit/test_admin_settings.py tests/unit/test_oauth_apps.py -q --cov=privacyfence.admin_settings --cov=privacyfence.oauth_apps --cov-branch --cov-report=term-missing passes and reports 100% for both modules'
      - 'python3 -m pytest tests/unit -q passes'
      - 'python3 -m mypy src/privacyfence/admin_settings.py src/privacyfence/oauth_apps.py passes'
      - 'grep -n "admin_settings.py\|oauth_apps.py" scripts/check_coverage_floor.py matches twice'
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
      1. Guard grep: `grep -rnE "(not|if|bool\()\s*org_config\b|len\(org_config\)" src/privacyfence`. The planner and the reviewer found only harmless hits: `org_config = org_config or {}` in daemon_main._maybe_start_web_server, and `org_config or {}` at routes_settings.py:1643. If any other hit decides something from org_config being empty or non-empty, stop with status=blocked and name it.
      2. daemon_main.py:
         - Add effective_org_config(bundle) per D3: an AdminSettingsError becomes org_mode.ConfigurationError.
         - In run_app, rename the load to `bundle = load_org_config()`, call reject_removed_tool_annotations(bundle), then set `org_config = effective_org_config(bundle)`. Pass `bundle` to log_org_config_bundle_hash; everything else keeps `org_config`.
         - At the CLI dispatch (`org_config = load_org_config()` near line 2518), use the mode="local" form from D3.
      3. Thread the raw bundle to org mode, per D3:
         - Add a `bundle` keyword to _maybe_start_web_server and to _start_org_web_server, and pass it from run_app through _maybe_start_web_server.
         - Add a field `bundle: dict[str, Any] = field(default_factory=dict)` to web/server.py's OrgAuth dataclass, set where OrgAuth is built.
         - Nothing reads it yet; p5a does.
      4. settings_controller.py: _org_config_or_empty (line ~1203) and refresh_connectors' work (line ~1240) call `effective_org_config(load_org_config())`. Import both from .daemon_main inside the function, as the existing code does, and keep the existing ConfigurationError handling.
      5. Tests.
         - test_daemon_main.py, class TestEffectiveOrgConfig:
           - With no bundle and a builtin slack app (monkeypatch app_credentials.builtin_app), the slack section is present.
           - A malformed admin_settings.json raises ConfigurationError.
           - The argument run_app passes to log_org_config_bundle_hash is the raw bundle: monkeypatch it and assert there are no synthesized sections.
           - The CLI form uses the local built-in keys (google_desktop) even when the bundle says mode org.
         - Next to TestBuildConnectorsSlack: build_connectors(config, effective_org_config({})) with a builtin slack app and a token file builds Slack.
         - test_settings_controller.py: with no bundle and a builtin miro app, the snapshot lists Miro and authenticate_connector("miro") no longer sets "Miro isn't enabled in your organization config."; with miro withdrawn in admin_settings, it does.
         - The conftest fixture from p2 already makes builtin_app return None by default, so existing "no bundle, no connectors" tests keep passing unchanged. Do not weaken any assertion.
      6. Run ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - 'python3 -m pytest tests/unit -q passes'
      - 'python3 -m pytest tests/unit/test_daemon_main.py -q -k TestEffectiveOrgConfig passes and selects at least 4 tests'
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
      - tests/unit/test_daemon_main.py
      - tests/conftest.py
      - scripts/check_coverage_floor.py
    brief: |
      Read "Design → D5": the contract, the host rules and "Org server side". Also read D6. The Worker is p6; here everything that talks to it is mocked.
      1. Guard grep: `grep -rnE "dict\(org_config\)|\{\*\*org_config|deepcopy\(org_config" src/privacyfence/daemon_main.py src/privacyfence/web`. The reviewer found none. If a copy of the org config is handed to routes_connect, build_org_routes, build_connectors or another long-lived object, stop with status=blocked and name it: refresh_in_place relies on one shared dict.
      2. Create src/privacyfence/relay_client.py per D5. It contains relay_token_path, well_known_token, valid_relay_host, public_host, register, status, request_check, _tick, start_registration_loop and reset_for_tests. Keep the module state in a small dataclass guarded by a threading.Lock, and never log the token.
      3. tests/conftest.py: call relay_client.reset_for_tests() inside _reset(), and extend the D6 fixture to patch relay_client.relay_token_path.
      4. web/routes_connect.py:
         - Add `redirect_uri: str` to the pending-attempt record; `_PendingAuthStore.put` gains a `redirect_uri` keyword.
         - In start(), choose redirect_uri and state per D5 when oauth_apps.uses_relay(org_config.get(_ORG_CONFIG_SECTION[service]) or {}). Refuse with the existing /connect?error= redirect when relay_client.status()["last_ok_at"] is None.
         - In callback(), use pending.redirect_uri for the exchange instead of rebuilding it.
         - Strip RELAY_MARKER from the Google section before google_oauth.web_client_config, in both _build_authorize_url and _exchange_and_save.
      5. web/server.py: in the org-mode route assembly, right before the routes_connect.build_routes call, add Route("/.well-known/privacyfence-relay", ...) per D5.
      6. daemon_main.py _start_org_web_server: after server.start() (line ~1249), call relay_client.start_registration_loop(server_config.issuer_url, org_config, _shutdown_event), then relay_client.request_check().
      7. Tests.
         - tests/unit/test_relay_client.py:
           - The token is created once, is 43 characters, has mode 0600, and is regenerated when corrupt.
           - valid_relay_host and public_host over every example in D5's host rules.
           - register refuses an http issuer_url, one with a path, and a non-public host, without calling requests.post.
           - register posts exactly the specified JSON with timeout=10 and allow_redirects=False (monkeypatch requests.post).
           - Responses: 200 gives ok; 403 with {"error":"proof_failed"} gives detail "proof_failed"; a 500 gives "HTTP 500"; a RequestException gives "could not reach oauth.privacyfence.eu".
           - The token never appears in caplog.
           - _tick returns: not needed → no call, 3600; registered 2 h ago → no call, 3600; registered 25 h ago → call; never registered → call; a first failure → 60; a second → 120; capped at 3600; a success after a failure → 3600 with last_error cleared.
           - request_check wakes a loop that is waiting.
         - tests/unit/web/test_routes_connect.py, a TestRelay class:
           - With a section carrying "relay": True and a successful registration, /oauth/start/slack redirects to slack.com with redirect_uri=https://oauth.privacyfence.eu/callback/slack and a state starting "<netloc>~".
           - The callback exchanges with that same redirect_uri.
           - jira uses /callback/atlassian.
           - A Google relayed start passes no "relay" key into web_client_config.
           - With no successful registration, start redirects to /connect?error=slack.
           - A bundle section without the marker keeps today's redirect_uri.
         - tests/unit/web/test_server_org_mode.py: GET /.well-known/privacyfence-relay without a session returns 200 text/plain, with the token and Cache-Control: no-store.
         - tests/unit/test_daemon_main.py: _start_org_web_server starts the registration loop. Monkeypatch relay_client.start_registration_loop and assert it was called with the shared org_config object (`is`).
      8. scripts/check_coverage_floor.py: add "src/privacyfence/relay_client.py" to MODULE_FLOORS at 100.
      9. Run ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - 'python3 -m pytest tests/unit -q passes'
      - 'python3 -m pytest tests/unit/web/test_routes_connect.py -q -k TestRelay passes and selects at least 6 tests'
      - 'python3 -m pytest tests/unit/test_relay_client.py -q --cov=privacyfence.relay_client --cov-branch --cov-report=term-missing passes and reports 100%'
      - 'ruff check . passes'
  - id: p5a-settings-apps-backend
    title: Settings actions, snapshot rows and capabilities for Settings → Apps in both modes
    depends_on: [p4-org-relay-client]
    complexity: M
    touches:
      - src/privacyfence/web/org_settings_scope.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/connector_registry.py
      - src/privacyfence/web/server.py
      - tests/unit/web/test_org_settings_scope.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_connector_registry.py
    brief: |
      Read "Design → D4": the actions table, Local mode, Org mode, evict_all and Snapshot rows. The UI is p5b. The pattern to copy for an admin_only action in both modes is toggle_pii_detection: its ACTION_SCOPES entry, _SENSITIVE_ACTIONS, _ORG_ALLOWED_ACTIONS and its _apply_org_action branch, added for org mode in commit 8adeb5e3.
      1. connector_registry.py: add evict_all() per D4, with a test in tests/unit/test_connector_registry.py.
      2. org_settings_scope.py: add the four actions with ActionScope(frozenset({LOCAL_MODE, ORG_MODE}), admin_only=True).
      3. routes_settings.py:
         - Put offer_service, set_service_app and clear_service_app in _SENSITIVE_ACTIONS, and withdraw_service in _NON_SENSITIVE_ACTIONS.
         - Add all four to _ORG_ALLOWED_ACTIONS.
         - Add the build_org_routes keyword `bundle: dict[str, Any] | None = None`, and pass org.bundle from web/server.py's build_org_routes call.
         - Add the _apply_org_action branch per D4: apply_change; refresh_in_place on the org_config dict build_org_routes already receives; connector_registry.evict_all() when connector_registry is not None; relay_client.request_check(); the AdminSettingsError → PolicyChangeRejected mapping; the OSError log line; and the summary plus " (admin=<id>)".
         - Add "services" and "relay_status" to _org_state per D4.
      4. settings_controller.py: add the four methods per D4, with set_service_app's login_url defaulting to "". snapshot() gains "services", built by admin_settings.service_rows from:
         - the raw bundle: load_org_config(), or {} on ConfigurationError;
         - the admin settings: admin_settings.load(), or {"version": 1, "services": {}} on AdminSettingsError;
         - mode="local".
      5. Tests.
         - test_org_settings_scope.py: the four actions are admin_only in both modes. Extend the existing parametrization; do not add special cases.
         - test_routes_settings.py:
           - TestSensitiveActionsCoverAllAllowedActions still passes.
           - Local set_service_app requires step-up when step-up is required (copy an existing TestSensitiveActionStepUp case).
           - A set_service_app body without login_url is accepted.
           - The response never contains the posted secret: assert the secret string is absent from the whole response body.
         - test_routes_org_settings.py:
           - A non-admin gets 403 for each action.
           - An admin's set_service_app changes the shared org_config dict's section in place, calls evict_all and request_check, and writes an audit entry whose summary contains the client id and not the secret.
           - withdraw_service removes the section.
           - An invalid service gives the PolicyChangeRejected error response with "Unknown service.".
           - _org_state for a non-admin has services == [].
         - test_settings_controller.py: each method's effect and its error path.
      6. Run ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if tests/unit/web/test_org_settings_scope.py forbids an admin_only action in both modes for a reason this brief does not cover, stop with status=blocked.
    acceptance:
      - 'python3 -m pytest tests/unit -q passes'
      - 'grep -n ''"set_service_app"'' src/privacyfence/web/org_settings_scope.py src/privacyfence/web/routes_settings.py matches in both files'
      - 'ruff check . passes'
  - id: p5b-settings-apps-ui
    title: The Apps section of the Settings page
    depends_on: [p5a-settings-apps-backend]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read "Design → D4": Capabilities, "Switching apps signs people out of that service" and the UI subsection. Every string is there.
      1. Add "apps" to _ALL_SECTIONS (settings_window_html.py:2502), set it to is_admin in _capabilities_for's org branch (:2525), and add an "Apps" nav item, guarded by CAPS.sections.apps the way the other sections are guarded.
      2. Add renderApps(state) and call it from the section switch (:1874-1884) for "apps":
         - One card per state.services row, with the toggle built by toggleHtml(row.offered, row.offered ? 'withdraw_service' : 'offer_service', {service: row.key}, row.locked, ...).
         - The status line per D4.
         - In org mode, the relay status line from state.relay_status.
      3. The "Use your own app" form: copy the structure of the Grist API-key modal at settings_window_html.py:1970-2020 (open it, inputs, submit through post(), close and clear on success).
         - Fields: Client ID; Client secret (type="password" autocomplete="off"); and for salesforce, "Login URL (optional)".
         - Action: set_service_app with {service, client_id, client_secret, login_url}.
         - It carries D4's "Everyone who already connected <Label> will need to connect again." line.
         - The secret input is cleared after every submit, whether it succeeded or not.
      4. The "Use the built-in app" button posts clear_service_app {service}, shown only when row.source == "admin", with the same reconnect line next to it.
      5. Tests in tests/unit/test_settings_window_html.py:
         - Extend the section-set tests at :908-959 so "apps" is in local mode, in org mode for an admin, and absent in org mode for a non-admin.
         - Every D4 status string is rendered for its row source. These tests must call renderApps through the same JS-evaluation helper the file's existing render tests use.
         - The secret input has type="password" and autocomplete="off".
         - The Salesforce card has the Login URL input and the others do not.
         - A locked row's toggle is disabled and has no form.
      6. Run ruff check . and python3 -m pytest tests/unit/test_settings_window_html.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py -q. If the hook's Chromium line said no WARNING, also run python3 -m pytest tests/integration/test_browser_smoke.py -q.
    acceptance:
      - 'python3 -m pytest tests/unit/test_settings_window_html.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py -q passes'
      - 'grep -n "function renderApps" src/privacyfence/settings_window_html.py matches'
      - 'grep -n "will need to connect again" src/privacyfence/settings_window_html.py matches'
      - 'ruff check . passes'
  - id: p6-relay-worker
    title: The OAuth redirect relay Worker at oauth.privacyfence.eu and its deploy workflow
    depends_on: []
    complexity: M
    touches:
      - cloudflare/oauth-relay/**
      - .github/workflows/deploy-oauth-relay-worker.yml
      - .github/workflows/dependency-audit.yml
      - .github/dependabot.yml
      - .claude/toolkit.yaml
    brief: |
      Read "Design → D5": the contract, the host rules and "The Worker". Routes, codes, texts and headers are all there.
      1. Create cloudflare/oauth-relay/:
         - Copy cloudflare/downloads' package.json, renamed privacyfence-oauth-relay, with the same scripts, devDependencies and overrides.
         - Copy its tsconfig.json and vitest.config.ts. Drop readD1Migrations and TEST_MIGRATIONS, and keep the coverage thresholds.
         - Write wrangler.toml per D5: the Durable Object REGISTRY, class Registry, migration tag v1 with new_sqlite_classes.
         - Run `npm install` there to produce package-lock.json with the same versions as cloudflare/downloads/package-lock.json.
      2. Source files:
         - src/index.ts: routing and headers.
         - src/registry.ts: the Durable Object class Registry, extending DurableObject from "cloudflare:workers". It creates the table in its constructor with ctx.storage.sql, and has the methods getRegistration(host), noteAttempt(host, nowIso), upsertVerified(host, nowIso), revoke(host) and listActive().
         - src/validate.ts: the host rules, plus token, grant and state parsing.
         - worker-configuration.d.ts: `interface Env { REGISTRY: DurableObjectNamespace<import("./src/registry").Registry>; REVOKE_TOKEN: string; }`.
         - For POST /admin/revoke, compare constant-time by comparing the SHA-256 digests of both values with crypto.subtle.timingSafeEqual.
      3. cloudflare/oauth-relay/README.md: what the relay does; the contract from D5; how a maintainer revokes a host (the curl call to /admin/revoke with the bearer token); and that the token comes from the GitHub environment secret CF_OAUTH_RELAY_REVOKE_TOKEN.
      4. Tests, in test/*.test.ts with the workers pool and calling the Worker's fetch handler the way cloudflare/downloads/test/download.test.ts does. Stub the outbound proof fetch with `vi.spyOn(globalThis, "fetch")`. Cover:
         - GET and HEAD /health.
         - The register happy path.
         - Every host case listed in D5's host rules: invalid → 400 invalid_request; not public → 400 host_not_public.
         - A proof mismatch, a 404 and a 302 → 403 proof_failed.
         - Revoked → 403 revoked.
         - A second register within 60 s → 429 too_soon.
         - The callback forwards with the exact original query to https://<host>/oauth/callback/<grant>.
         - An unknown grant → 404.
         - Unregistered, revoked, and verified 36 days ago → 400 with the exact text.
         - /registrations lists non-revoked, verified hosts, sorted.
         - Every response has the three headers.
         - /admin/revoke gives 401 with no token and with a wrong token, and 200 with the right one.
      5. .github/workflows/deploy-oauth-relay-worker.yml per D5. Copy deploy-download-worker.yml with jobs verify-oauth-relay and deploy-oauth-relay, no D1 step, and the "Set revoke token" step.
      6. .github/workflows/dependency-audit.yml: add a job npm-audit-oauth-relay, copied from npm-audit-worker with working-directory cloudflare/oauth-relay. Add cloudflare/oauth-relay/package.json and package-lock.json to the same paths filters that list the downloads Worker's files.
      7. .github/dependabot.yml: add an npm entry for directory "/cloudflare/oauth-relay", copied from the "/cloudflare/downloads" entry (line ~51), and extend the comment at the top that names the Worker.
      8. .claude/toolkit.yaml verify.dod_conditional: add an entry `when: ["cloudflare/oauth-relay/**"]`, with `do: "In cloudflare/oauth-relay/: \`npm test\`, \`npm run typecheck\` and \`npm run dry-run\` must pass."`.
      9. In cloudflare/oauth-relay, run: npm ci, npm run typecheck, npm test, npm run dry-run.
      Stop conditions:
      - If the vitest workers pool version pinned in cloudflare/downloads cannot run a SQLite Durable Object (an error naming new_sqlite_classes or ctx.storage.sql), stop with status=blocked. Do not switch to D1: D1 needs a dashboard-provisioned id this plan does not have.
      - If vi.spyOn(globalThis, "fetch") does not intercept the Worker's outbound fetch in the pool, stop with status=blocked.
    acceptance:
      - 'cd cloudflare/oauth-relay && npm ci && npm run typecheck && npm test && npm run dry-run passes'
      - 'grep -n "oauth.privacyfence.eu" cloudflare/oauth-relay/wrangler.toml matches'
      - 'grep -n "enabled = false" cloudflare/oauth-relay/wrangler.toml matches'
      - 'grep -n "cloudflare/oauth-relay" .claude/toolkit.yaml .github/workflows/dependency-audit.yml .github/dependabot.yml matches in all three'
  - id: p7-adrs
    title: ADRs 0239-0242 and the status line on 0196
    depends_on: [p5b-settings-apps-ui, p6-relay-worker]
    complexity: S
    touches:
      - docs/adr/0239-built-in-oauth-apps-ship-in-every-distribution.md
      - docs/adr/0240-an-apps-credentials-resolve-bundle-then-admin-then-built-in.md
      - docs/adr/0241-admin-settings-live-outside-the-signed-bundle.md
      - docs/adr/0242-org-servers-use-built-in-apps-through-a-domain-verified-relay.md
      - docs/adr/README.md
      - docs/adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md
    brief: |
      1. Check `ls docs/adr | tail -3` first. If 0239-0242 are taken by ADRs merged to main since this plan was written, take the next free numbers and use them everywhere; p8a and p8b read the file names from docs/adr/.
      2. Write the four ADRs in docs/adr/README.md's template: Status (Accepted, today's date, Implemented), Context, Decision, Alternatives considered, Consequences, Verification, Related.
         - Their decisions are this plan's "ADRs" section. Their context, alternatives and consequences come from the Goal, Design D1-D6 and "Accepted risks".
         - Alternatives to record:
           - 0239: fetch the credentials from a PrivacyFence endpoint at runtime; have the relay broker the token exchange and hold the secrets.
           - 0240: keep requiring a bundle section (ADR 0196 as is); let admin settings override a bundle section; turn every service on for upgraded installs too.
           - 0241: let the UI rewrite org_config.json (it cannot be re-signed); keep overrides in settings.yaml (per-principal, not install-wide); let any local account change them.
           - 0242: no relay (org servers register their own apps, as before); a maintainer-approved list of servers; forwarding to any server that asks.
         - Consequences to record: switching apps makes people reconnect Salesforce, Atlassian and Miro (D4); the provider caveats in Risks.
         - Link source files, never this plan. Do not mention any model or agent.
      3. ADR 0196: add one line to its Status section only, "Amended by [0240](0240-….md)." ADR 0040 is not changed.
      4. docs/adr/README.md: add four index rows in the existing format:
         - 0239: "Accepted; relates to 0040"
         - 0240: "Accepted; amends 0196"
         - 0241: "Accepted; relates to 0016, 0034, 0070, 0200"
         - 0242: "Accepted; relates to 0011, 0186"
      5. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q.
    acceptance:
      - 'ls docs/adr/0239-*.md docs/adr/0240-*.md docs/adr/0241-*.md docs/adr/0242-*.md lists four files (or the renumbered four)'
      - 'python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q passes'
      - 'git diff origin/main --stat -- docs/adr/0040-telegram-app-credentials-ship-in-every-distribution.md prints nothing'
  - id: p8a-docs
    title: Reference docs and changelog for built-in apps, Settings → Apps and the relay
    depends_on: [p7-adrs]
    complexity: M
    touches:
      - docs/getting-started.md
      - docs/connecting-a-service.md
      - docs/org-mode-setup-guide.md
      - docs/org-mode-docker.md
      - docs/configuration-reference.md
      - docs/google-cloud-setup.md
      - docs/slack-setup.md
      - docs/salesforce-setup.md
      - docs/atlassian-setup.md
      - docs/miro-setup.md
      - docs/telegram-setup.md
      - docs/security-and-compliance.md
      - CHANGELOG.md
    brief: |
      Reference docs describe today's behaviour and link the ADRs p7 wrote (take their file names from docs/adr/) for the why.
      1. getting-started.md ("Connect your services") and connecting-a-service.md:
         - A fresh install offers Google, Slack, Salesforce, Atlassian, Miro and Telegram with built-in apps and no bundle.
         - Settings → Apps: who may use it, step-up, and the status strings from D4.
         - A bundle section still wins and shows locked. An upgraded install whose bundle lists connectors keeps that list until an admin switches more on.
         - Switching apps makes people reconnect.
         - Keep the bundle-install section, now described as optional.
      2. Each provider setup doc (google-cloud-setup, slack-setup, salesforce-setup, atlassian-setup, miro-setup):
         - Open with "You need this page only to use your organization's own app instead of the built-in one."
         - Say how to enter the app in Settings → Apps as well as by bundle.
         - Give the provider caveat:
           - Google: an unverified-app screen and a 100-user cap until verification, and verification of restricted Gmail and Drive scopes includes an annual security assessment. Workspace admins can trust the client ID.
           - Salesforce: orgs that block uninstalled connected apps need an admin to install or approve it.
           - Slack: workspaces that require app approval need an admin's approval once.
           - Atlassian and Miro: nothing extra.
         - google-cloud-setup.md also says an org-mode override must be a Web application client.
         - telegram-setup.md describes the Settings → Apps switch.
      3. org-mode-setup-guide.md:
         - §5 "Connector apps":
           - Built-in apps through the relay are the default for services the bundle does not list.
           - What the relay is.
           - The requirements: an https issuer_url with no path, and a server reachable from the internet at https://<server>/.well-known/privacyfence-relay. With §7's IP restriction, allow that path or use your own apps.
           - Outbound HTTPS to oauth.privacyfence.eu.
           - The 35-day re-verification and the public /registrations list.
           - How to read the registration status in Settings → Apps.
         - §15 Troubleshooting: rows for "Relay registration failed: proof_failed", "host_not_public", "revoked", "too_soon", "the relay needs an https issuer_url with no path", and the relay's 400 text from D5.
      4. org-mode-docker.md: one note that the container needs outbound HTTPS to oauth.privacyfence.eu when it uses built-in apps.
      5. configuration-reference.md: org/admin_settings.json (the schema from D2; written only by Settings, not to be hand-edited); org/relay_token.txt; and the PRIVACYFENCE_BUILTIN_APPS development variable and its JSON shape.
      6. security-and-compliance.md: a short "Built-in apps and the relay" subsection:
         - Built-in secrets are public in effect (ADR 0239).
         - What the relay sees (the authorization code in transit, never stored or logged) and what it does not (no tokens).
         - The domain-proof trust model (ADR 0242).
         - admin_settings.json has settings.yaml-level trust (ADR 0241).
      7. CHANGELOG.md under "## [Unreleased]": one entry each for built-in apps on desktop installs, Settings → Apps, and organization servers using built-in apps through the relay (including the upgrade rule). No version heading.
      8. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_pages.py -q and ruff check ..
    acceptance:
      - 'python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_pages.py -q passes'
      - 'grep -n "oauth.privacyfence.eu" docs/org-mode-setup-guide.md docs/security-and-compliance.md docs/org-mode-docker.md matches in each file'
      - 'grep -n "Settings → Apps" docs/connecting-a-service.md docs/slack-setup.md matches in both'
      - 'git diff origin/main -- CHANGELOG.md | grep -c "^+## \[" prints 0'
  - id: p8b-website
    title: Website pages and privacy policy for built-in apps and the relay
    depends_on: [p7-adrs]
    complexity: M
    touches:
      - website/privacy/index.html
      - website/connectors/index.html
      - website/connectors/google-workspace/index.html
      - website/connectors/jira-confluence/index.html
      - website/connectors/salesforce/index.html
      - website/connectors/slack/index.html
      - website/connectors/miro/index.html
      - website/enterprise/index.html
      - tests/unit/test_website_claims.py
    brief: |
      1. website/privacy/index.html:
         - Extend the scope sentence (lines ~26-27) to name oauth.privacyfence.eu.
         - Add a section "Sign-in relay (oauth.privacyfence.eu)" after the download section. It says the relay:
           - is a small Cloudflare Workers service that forwards an organization server's sign-in redirect;
           - processes the IP address and the request in transit;
           - stores only each registered server's host name and registration dates, shown publicly at /registrations;
           - keeps no request logs;
           - is used only by organization servers that use the built-in apps.
           The legal basis is Art. 6(1)(f), as for hosting.
         - Change "sends nothing to the project" (lines ~45-54) to say the one exception is an organization server's registration of its host name with the relay.
         - Update "Last updated" to the day you make the change.
      2. The pages that say an administrator must register an app and ship a bundle say instead that PrivacyFence ships its own app and an organization may use its own:
         - website/connectors/index.html:203
         - google-workspace/index.html:155-156
         - jira-confluence/index.html:117-118
         - salesforce/index.html:92-93
         - slack/index.html:95-96: remove "The app is never publicly distributed".
         - miro/index.html:61
         - website/enterprise/index.html:85
         Keep each change to those sentences.
      3. If tests/unit/test_website_claims.py pins a sentence you changed, update that test's expected text to the new sentence, and nothing else.
      4. Run python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_build_site.py -q. If the hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_website_layout.py tests/integration/test_website_consent.py -q.
    acceptance:
      - 'python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_build_site.py -q passes'
      - 'grep -n "oauth.privacyfence.eu" website/privacy/index.html matches'
      - 'grep -rn "never publicly distributed" website/connectors/slack/index.html finds nothing'
  - id: p9-retire-plan
    title: Delete the plan document and its manual-steps page
    depends_on: [p8a-docs, p8b-website]
    complexity: S
    touches:
      - docs/builtin-oauth-apps-plan.md
      - docs/builtin-oauth-apps-plan-manual-steps.html
    brief: |
      The decisions were extracted into ADRs in p7.
      1. Delete docs/builtin-oauth-apps-plan.md and docs/builtin-oauth-apps-plan-manual-steps.html.
      2. Run grep -rn "builtin-oauth-apps-plan" --exclude-dir=.git . and remove any remaining reference.
      3. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q.
    acceptance:
      - 'test ! -e docs/builtin-oauth-apps-plan.md && test ! -e docs/builtin-oauth-apps-plan-manual-steps.html'
      - 'grep -rn "builtin-oauth-apps-plan" --exclude-dir=.git . finds nothing'
      - 'python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q passes'
```
