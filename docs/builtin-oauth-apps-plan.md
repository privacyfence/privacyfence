# Plan: PrivacyFence 7 — split the organization config, an Admin page, built-in OAuth apps and single-user Docker mode

## Goal

Today `org_config.json`, a signed bundle built offline with `scripts/build_org_bundle.py`, carries
everything an administrator controls:

- the startup settings: `mode`, `server`, `idp`;
- the connector app credentials;
- which connectors are offered (ADR 0196);
- operational policy: file delivery, web push, sign-in restrictions, step-up, scoped tokens, audit
  forwarding, unattended sessions and the room directory.

Changing any of it means rebuilding, re-signing and reinstalling the bundle. A fresh install offers
no connector at all until an administrator registers their own OAuth apps.

After this plan:

- **`org_config.json` holds only the startup settings and app credentials.** These are `mode`,
  `server`, `idp`, and the client IDs and secrets for Google, Slack, Salesforce, Atlassian, Miro and
  a Grist OAuth app. It stays a signed bundle (ADR 0016, 0200). A provider the bundle does not
  carry uses the **built-in PrivacyFence-owned app** baked into every distribution, as Telegram's
  app credentials are (ADR 0040).
- **Everything operational moves to a new admin config**, `org/admin_config.json`. The daemon
  writes it, and administrators edit it on a new top-level **Admin** page:
  - which services are offered;
  - the Peek and Grist server addresses, Jira Assets, the room directory and unattended sessions;
  - file delivery and web push;
  - sign-in restrictions, step-up, scoped tokens and audit forwarding;
  - the install-wide privacy policy and the usage stats (org mode).
- **Settings becomes personal for everyone.** An org admin's Settings page shows the same sections a
  normal user's does. The admin-only sections (Privacy, Stats) move to Admin.
- **Who may use Admin:** an org admin is an IdP admin (`Principal.is_admin`); on a desktop install
  it is the install owner. Sensitive changes need step-up, as sensitive settings changes do today.
- **Upgrades keep working.** On the first start after the upgrade, the operational sections of an
  existing `org_config.json` are imported once into the admin config. From then on they are ignored
  in the bundle, and a startup warning names them. A bundle that listed connectors keeps exactly
  that list offered.
- **Org servers use the built-in apps through a relay.** They go through a new PrivacyFence-hosted
  OAuth redirect relay at `https://oauth.privacyfence.eu`. It forwards a provider's redirect only to
  a server that has proven it controls its own domain. Desktop installs use the built-in apps
  directly through their fixed loopback redirects.

- **The Docker image starts in organization mode on its own.** With no bundle and no IdP it runs
  as single-user mode, with one administrator, on `http://localhost`. Sign-in uses a one-time setup code from the
  container log, then a passkey. Built-in apps reach it through the relay's loopback forwarding.
  `mode` and the server address can also come from environment variables.
- **A server can start from environment variables alone.** The IdP and the server address can
  come from `privacyfence.env`, and admins can be listed by email. A multi-user server's bootstrap
  is then DNS, one OIDC app at the IdP, an env file and `docker compose up -d`.
- **The upgrade can be undone.** 7.x copies `org_config.json` to `org_bundle.json`, and its pin
  files to new names, and never touches 6.x's files. Going back to the latest 6.x keeps working
  with the `org_config.json` it had.
- **7.x is the migration checkpoint.** Every 7.x start upgrades every person's stored rules and
  tokens, not only those of people who sign in. Once nothing is left to convert, it writes a
  `data_format.json` marker. 8.x can then drop all the old upgrade code and refuse data without
  the marker, with "install 7.x first". The Admin page says whether an install is ready for 8.
- **Desktop (local) mode is deprecated.** 7.x says so in the daemon log, in `privacyfence_status`,
  on every local page and once from the companion. 8.x removes it, in a separate plan written after
  7.0 ships. The release is cut as 7.0.0 by the maintainer after this plan's PR merges.

An organization that wants its own app for a provider puts the credentials in the bundle, as today.
The Admin page shows, for each service, which app is in use and how to switch.

Grist and Peek have no built-in app. Their servers and OAuth apps belong to each organization.

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
- **ADR numbers.** The highest accepted ADR is 0238, so this plan takes 0239 to 0249.
- **Operational sections and their parsers.** Each takes the whole org config dict:
  - `download_delivery`: `org_mode.DownloadDeliveryConfig.from_org_config`
  - `audit_forwarding`: `org_mode.AuditForwardingConfig.from_org_config`
  - `authz`: `org_mode.AuthzPolicyConfig.from_org_config`
  - `web_push`: `org_mode.WebPushConfig.from_org_config`
  - `step_up`: `step_up_config.StepUpConfig.from_org_config` (`step_up_config.py:287`)
  - `scoped_tokens`: `scoped_tokens.policy_from_org_config` (`scoped_tokens.py:180`)
  - `unattended_sessions.enabled`: `daemon_main.py:2140`
  - `rooms`: `daemon_main.py:1456`, written into the bundle by `scripts/sync_room_directory.py`
  - `telegram`, `peek` (`base_url`), `grist` (`server_url`, plus the OAuth app fields) and the
    Atlassian flags `assets`/`assets_workspace_id`
- **When they are read.** In org mode most of these are parsed once at startup in
  `_start_org_web_server` and `web/server.py`'s org route assembly, so a change needs a restart.
  Connector sections and `rooms` are re-read whenever a principal's connectors are rebuilt.
- **`scripts/build_org_bundle.py`** (994 lines) has a flag for every section. Among them:
  - `--enable-telegram/-peek/-grist`;
  - the `--authz-*`, `--step-up-*`, `--unattended-*`, `--scoped-tokens-*` and download flags;
  - `--no-web-push` and `--audit-forwarding-*`.
  Its test is `tests/unit/test_build_org_bundle.py`.
- **The org Settings page** (`routes_settings.build_org_routes`) shows two admin-only sections:
  - Privacy: `toggle_pii_detection`, `toggle_pii_category`, `set_default_policy` and
    `set_category_policy`, applied by `org_install_policy.apply_change`.
  - Stats: `org_stats.collect`, ADR 0217.
  - Both are chosen by `settings_window_html._capabilities_for` (`:2525`).
- **Local mode** has no admin concept. Its whole Settings surface is owner-only (`web/server.py`'s
  `_owner_only_routes`, `:835-854`).
- **The nav.** `web_shell._NAV_ITEMS` (local) and `web_shell.ORG_NAV_ITEMS` are static tuples
  (`web_shell.py:506-518`). `web_shell.wrap` treats `nav_items == ORG_NAV_ITEMS` as "org mode"
  (`:778`).
- **Settings action invariants.** `tests/unit/web/test_org_settings_scope.py` pins every settings
  action to have a local-mode route and forbids org-only actions. The Admin page therefore gets
  its own dispatcher rather than more `ACTION_SCOPES` entries.

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

### D2. What stays in `org_config.json`

From 7.x on, this file is `org/org_bundle.json`, adopted from `org_config.json` by copying (D13).
"Bundle" and `org_config.json` elsewhere in this plan mean that file.

`org_config.json` stays a signed bundle, with the same loading, signing, serial and hash-log rules
(`daemon_main.load_org_config`, ADR 0016, 0200). Its meaningful keys are now:

| Key | Contents |
|---|---|
| `mode`, `server`, `idp` | unchanged (startup settings) |
| `google`, `slack`, `salesforce`, `miro` | the app credentials, unchanged field names |
| `atlassian` | `client_id`, `client_secret` only |
| `grist` | the OAuth app only: `client_id`, `client_secret`, `auth_server_url`, `server_url` (the server the app belongs to, ADR 0143) |
| bookkeeping | `version`, `org_name`, `serial`, `signature`, `signing_public_key`, and whatever `org_bundle_signing` adds |
| `mcp` | still refused when it carries `tool_annotations` (ADR 0089) |

**Legacy operational keys.** `admin_config.LEGACY_BUNDLE_KEYS` is this tuple:
`("download_delivery", "web_push", "authz", "step_up", "scoped_tokens", "audit_forwarding",
"unattended_sessions", "rooms", "rooms_synced_at", "telegram", "peek")`. Three legacy sub-keys
are also treated as operational:

- `atlassian.assets`
- `atlassian.assets_workspace_id`
- `grist.server_url` when `grist` has no `client_id` (the API-key server address)

All of these are imported once (D3) and afterwards ignored (D4). The function
`admin_config.legacy_keys_in(bundle) -> list[str]` returns the ones present, as dotted names (for
example `["authz", "atlassian.assets"]`).

### D3. The admin config

**The file.** `org/admin_config.json` (`admin_config.admin_config_path()` returns
`paths.org_dir() / "admin_config.json"` at call time). The daemon is its only writer, through
`secure_files.atomic_write_json` (mode 0600). Its trust level is settings.yaml's (the install
directory), not the signed bundle's.

Schema, version 1:

```json
{"version": 1,
 "imported_from_bundle_sha256": "<hex, or \"\" when there was no bundle>",
 "services": {
   "google":     {"offered": true},
   "slack":      {"offered": true},
   "salesforce": {"offered": true},
   "atlassian":  {"offered": true, "assets": false, "assets_workspace_id": ""},
   "miro":       {"offered": true},
   "telegram":   {"offered": true},
   "grist":      {"offered": false, "server_url": ""},
   "peek":       {"offered": false, "base_url": ""}},
 "rooms": [], "rooms_synced_at": "",
 "unattended_sessions": {"enabled": false},
 "download_delivery": {}, "web_push": {}, "authz": {}, "step_up": {},
 "scoped_tokens": {}, "audit_forwarding": {}}
```

- `SERVICES = ("google", "slack", "salesforce", "atlassian", "miro", "telegram", "grist", "peek")`
- `SERVICE_LABELS = {"google": "Google", "slack": "Slack", "salesforce": "Salesforce", "atlassian":
  "Atlassian", "miro": "Miro", "telegram": "Telegram", "grist": "Grist", "peek": "Peek"}`
- `OPERATIONAL_SECTIONS = ("rooms", "rooms_synced_at", "unattended_sessions", "download_delivery",
  "web_push", "authz", "step_up", "scoped_tokens", "audit_forwarding")`. Each is stored in exactly
  the shape `org_config.json` used for it, so the existing parsers read it unchanged (D4).

**Module `src/privacyfence/admin_config.py`.**

- `class AdminConfigError(ValueError)`.
- `load() -> dict[str, Any] | None`:
  - A missing file gives `None`.
  - Unreadable or malformed JSON, a non-object, or `version != 1` raises `AdminConfigError`. The
    message names the path and never the content.
- `save(data) -> None` writes atomically.
- `import_from_bundle(bundle: dict[str, Any], *, mode: Mode, single_user: bool) -> dict[str, Any]`
  builds a fresh config:
  - **`offered`, when the bundle has a non-empty section for any service in `SERVICES`:** each
    service's `offered` is whether its section is non-empty, as ADR 0196 had it.
  - **`offered`, otherwise:**
    - In local mode, and in an org-mode single-user install (D9), google, slack, salesforce, atlassian, miro and
      telegram are `true`.
    - On a multi-user org server they are all `false`. The first service an admin offers on the
      Admin page is the opt-in, and that is what starts relay registration.
    - grist and peek are always `false` here, because they need a server address.
  - `atlassian.assets`/`assets_workspace_id`, `peek.base_url`, and `grist.server_url` (only when
    the bundle's grist has no `client_id`) are copied from the bundle.
  - Each `OPERATIONAL_SECTIONS` key present in the bundle is copied verbatim.
  - `imported_from_bundle_sha256` is the SHA-256 hex of
    `json.dumps(bundle, sort_keys=True, separators=(",", ":"))`, or `""` for an empty bundle.
- `ensure(bundle, *, mode, single_user) -> tuple[dict[str, Any], bool]`:
  - It returns `(loaded, False)` when the file exists, except in one case.
  - **The exception:** the loaded `imported_from_bundle_sha256` is `""`, meaning the config was
    created with no bundle, and `legacy_keys_in(bundle)` is non-empty. That is an IT bundle
    arriving after the first start. Then it re-imports, saves and returns `(new, True)`. This
    happens at most once, because the new config records the bundle's hash.
  - With no file, it saves and returns `(import_from_bundle(...), True)`.
- `config_for(bundle, *, mode, single_user) -> dict[str, Any]` returns `load()` when the file exists,
  otherwise `import_from_bundle(...)`, without writing. Readers that run without the daemon use it:
  the CLI and `SettingsController`. A missing file then never changes what the bundle offers.
- `load()` normalizes on read: a missing service gets the `import_from_bundle` default for it, and a
  missing key inside a service gets its schema default. A hand-edited file missing a service
  therefore never raises `KeyError`.
- `service_rows(bundle, admin, *, mode)`: see D6.
- `apply_change(action: str, payload: dict[str, Any]) -> tuple[str, list[str]]`: see D6. A
  module-level `threading.Lock`, `_write_lock`, serializes it.
- No function in this module ever puts a value from `audit_forwarding.http.bearer_token_env`'s
  environment variable, or any client secret, into a message, a log line or a return value.

**Where the import runs.** `daemon_main.load_admin_config(bundle) -> tuple[dict[str, Any], bool]`
is called once in `run_app`, right after `bundle = load_org_config()`.

- It calls `admin_config.ensure(bundle, mode=org_mode.resolve_mode(bundle),
  single_user=org_mode.is_single_user(bundle))`. p4 lands before `is_single_user` exists (p15), so p4 passes
  `single_user=False` and p15 changes the call.
- On import it logs at WARNING `Imported <n> settings from org_config.json into
  org/admin_config.json; they are now managed on the Admin page`, where `<n>` is
  `len(admin_config.legacy_keys_in(bundle))`.
- Whenever `legacy_keys_in(bundle)` is non-empty and no import happened, it logs at WARNING
  `org_config.json carries settings that are now managed on the Admin page and are ignored:
  <names>. Rebuild the bundle without them.`
- `AdminConfigError` becomes `org_mode.ConfigurationError` (refuse to start).
- The audit entry is written by a separate
  `daemon_main.record_admin_config_import(bundle, imported: bool)`, called right after
  `log_org_config_bundle_hash(bundle)`, once the audit logger exists. When `imported` is true it
  records decision `admin_config_imported`, with the bundle's SHA-256 in its summary.

`settings_controller.install_org_config_bytes` (local bundle upload) sets the same "are ignored"
text in `self.error` when the uploaded bundle carries legacy keys. The controller has no separate
notice field, and the bundle is still installed.

### D4. The effective org config

All existing readers (`build_connectors`, the `_authenticate_*` methods, `routes_connect`,
`connector_enabled_by_org` and every `from_org_config` parser) keep reading one dict. It is now
composed from the bundle, the admin config and the built-in apps.

**Module `src/privacyfence/oauth_apps.py`:**

- `RELAY_BASE_URL = "https://oauth.privacyfence.eu"`, `RELAY_MARKER = "relay"`,
  `GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"`,
  `GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"`.
- `BUILTIN_KEY`, mapping `(service, mode)` to a built-in app key:

  | Service | local | org |
  |---|---|---|
  | `google` | `google_desktop` | `google_web` |
  | `slack` | `slack` | `slack` |
  | `salesforce` | `salesforce` | `salesforce` |
  | `atlassian` | `atlassian_local` | `atlassian_relay` |
  | `miro` | `miro` | `miro` |

- `AppSource = Literal["bundle", "builtin", "none"]`.
- `credentials_source(bundle, service, *, mode) -> AppSource` is defined for the five services
  above:
  - `"bundle"` when the bundle section has a `client_id`, or a `consumer_key` for salesforce;
  - `"builtin"` when `app_credentials.builtin_app(BUILTIN_KEY[(service, mode)])` is not None;
  - `"none"` otherwise.
- `effective_org_config(bundle, admin, *, mode) -> dict[str, Any]` returns a new dict and mutates
  neither input:
  1. Start from a shallow copy of `bundle` without the top-level `LEGACY_BUNDLE_KEYS`. Copy the
     `atlassian` section without `assets`/`assets_workspace_id`. Copy `grist` without `server_url`
     when it has no `client_id`. Drop `RELAY_MARKER` from every section.
  2. For each service in `SERVICES` whose `admin["services"][s]["offered"]` is false, remove its
     section.
  3. For each offered service, build its section:
     - (Read `offered` as `admin["services"].get(s, {}).get("offered", False)`; `load()` has
       already normalized it.)
     - **google, slack, salesforce, miro:** the bundle section when `credentials_source` is
       `"bundle"`.
       - Otherwise, with `"builtin"`, the built-in app's fields: `{"consumer_key",
         "consumer_secret"}` for salesforce, `{"client_id", "client_secret", "auth_uri",
         "token_uri"}` for google, and `{"client_id", "client_secret"}` for the rest.
       - In org mode the built-in section also gets `"relay": True`.
       - With `"none"`, no section.
     - **atlassian:** the credentials as above, plus `"assets"` from the admin config, plus
       `"assets_workspace_id"` when it is non-empty. With no credentials, no section.
     - **telegram:** `{"enabled": True}` when `app_credentials.telegram_app_credentials()` is not
       None, otherwise no section.
     - **grist:**
       - When the bundle `grist` section has a `client_id`, that section is used and the admin
         `server_url` is ignored.
       - Otherwise, if the admin `server_url` is non-empty: `{"server_url": <it>}`.
       - Otherwise, in local mode only: `{"enabled": True}`.
       - Otherwise no section.
     - **peek:**
       - If the admin `base_url` is non-empty: `{"base_url": <it>}`.
       - Otherwise, in local mode only: `{"enabled": True}`.
       - Otherwise no section.
  4. Copy each `OPERATIONAL_SECTIONS` key from `admin` when present.
- `refresh_in_place(target, new) -> None` deletes the keys of `target` that are not in `new`, then
  `target.update(new)`. In org mode one shared `org_config` dict is held by `OrgAuth`,
  `routes_connect`, `build_org_routes` and the connector factory. Each key assignment is atomic
  under the GIL.
- `relay_redirect_uri(grant) -> str` returns `f"{RELAY_BASE_URL}/callback/{grant}"`.
- `uses_relay(section) -> bool` returns `section.get(RELAY_MARKER) is True`.

**Where it is applied: `daemon_main.effective_org_config(bundle, admin) -> dict[str, Any]`.**

- It returns `oauth_apps.effective_org_config(bundle, admin, mode=org_mode.resolve_mode(bundle))`.
- `run_app` keeps the raw `bundle` for `reject_removed_tool_annotations`,
  `log_org_config_bundle_hash`, `resolve_mode`, `check_storage_permissions` and
  `init_privacy_filter`'s `org_managed`. Everything else gets
  `org_config = effective_org_config(bundle, admin)`.
- The CLI dispatch (`daemon_main.py:2518`) uses
  `oauth_apps.effective_org_config(bundle, admin_config.config_for(bundle, mode="local", single_user=False), mode="local")`,
  with `bundle = load_org_config()`.
  - The CLI OAuth runners are loopback flows and always use the local apps.
  - An `AdminConfigError` there exits the same way a `ConfigurationError` does.
- `settings_controller._org_config_or_empty` (`:1203`) and `refresh_connectors`'s `work`
  (`:1240`) call
  `effective_org_config(bundle, admin_config.config_for(bundle, mode=org_mode.resolve_mode(bundle), single_user=False))`,
  with `bundle = load_org_config()`. An `AdminConfigError` is handled like the
  `ConfigurationError` already caught there.
- In org mode the raw `bundle` travels down with a new `bundle` keyword:
  - `run_app` passes it to `_maybe_start_web_server(..., bundle=bundle)` (called at `:2160`);
  - that passes it to `_start_org_web_server(..., bundle=...)` (called at `:818-821`);
  - that stores it in a new `OrgAuth` field, `bundle: dict[str, Any]`.

  The Admin dispatcher (D6) needs it to recompute the effective config.
- The QA scripts (`scripts/qa_*.py`) are not changed. They read the QA runner's own bundle, which
  still carries `grist`/`peek` sections, and they ignore the admin config.

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
- The admin dispatcher (D6) calls `relay_client.request_check()` after every successful change to `services`.
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
  - A Durable Object needs no dashboard-provisioned ID, unlike a Cloudflare D1 database.
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
  - `GET /registrations` → 200 JSON `[{"host", "registered_at"}]` for non-revoked, verified hosts,
    sorted by host. It requires the maintainer token (`Authorization: Bearer <REVOKE_TOKEN>`) and
    answers 401 without it (D15 SEC-09). There is no public list.
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
  - no Cloudflare D1 migrations step; instead, after "Deploy Worker", a "Set revoke token" step that runs
    `printf '%s' "$REVOKE_TOKEN" | npx wrangler secret put REVOKE_TOKEN`, with step-level env
    `REVOKE_TOKEN: ${{ secrets.CF_OAUTH_RELAY_REVOKE_TOKEN }}` plus the two Cloudflare secrets;
  - smoke test `curl --head https://oauth.privacyfence.eu/health`;
  - job names `verify-oauth-relay` and `deploy-oauth-relay` (not `verify`, which the download Worker's workflow already uses).

**How the relay is released.** The relay is a service PrivacyFence runs, like the download
Worker. It is not an artifact operators install, and it ships on its own track, not with app
releases.

- **Production deploys.** `deploy-oauth-relay-worker.yml` deploys the Worker when a change under
  `cloudflare/oauth-relay/**` reaches `main`, or when someone runs the workflow on `main`.
- **Tags do not deploy it.** A release tag never changes the relay, and a relay change never needs
  an app release.
- **Order.** The relay must be live before the first release whose org servers use built-in apps,
  7.0.0. The feature PR merging to `main` deploys it, and `/cut-release` and `docs/releasing.md`
  gain a pre-flight line: `curl -fsSI https://oauth.privacyfence.eu/health` must return 200 before
  tagging.
- **Compatibility rule.** Every relay version must keep serving every released 7.x server. The D5
  contract (`/register`, `/callback/:grant`, the `state` format, the loopback rule) only grows:
  - a route or a field may be added;
  - an existing one is never changed or removed while a supported release uses it.

  A breaking change is a new path (for example `/v2/callback/...`) used by the new release, with
  the old path kept.
- **The maintainer runbook** is a new contributor doc, `docs/oauth-relay.md`. It is not published
  on the website (ADR 0051): add it to `scripts/build_site.py`'s `CONTRIBUTOR_DOCS` (`:168-182`)
  and to the contributor half of `docs/README.md`. It covers:
  - **Provisioning.** The zone, the token, the GitHub environment and its three secrets, and the
    first deploy.
  - **Checking a deploy.** These smoke tests:
    - `/health` returns 200;
    - `/registrations` is a JSON list;
    - `/register` for a host that cannot prove itself returns 403 `proof_failed`;
    - a loopback `/callback` returns a 302 to `http://localhost:…`;
    - an unregistered host's `/callback` returns 400.
  - **Revoking a host.** The curl call.
  - **Rotating `REVOKE_TOKEN`.** Update the environment secret, then run the workflow on `main`.
  - **Rolling back.** `npx wrangler rollback`, or the dashboard's Deployments tab.
  - **Reading the registrations.**
  - **The compatibility rule** above.
- **Cloudflare resources.** `docs/downloads-and-release-kpi.md`'s "Cloudflare resources" table
  gains rows for the Worker `privacyfence-oauth-relay`, the custom domain `oauth.privacyfence.eu`
  and the Durable Object class `Registry`. Its credentials table gains rows for the three
  `CF_OAUTH_RELAY_*` secrets.

**Accepted risks**, recorded in ADR 0243:

- Slack documents no PKCE, so a relayed Slack code passes through the Worker in the browser
  redirect. The Worker keeps no logs and the code is single-use.
- Anyone who owns a public domain can register a server and run sign-ins under the PrivacyFence
  name. Domain proof makes this cost a domain and a running server, and revocation removes a
  reported host.
- An org server not reachable from the internet, for example one restricted by IP address (setup
  guide §7), cannot register. It must use its own apps.

### D6. The Admin page

**Surface and access.**

- **Routes.** `GET /admin` (the page), `POST /api/admin/{action}` (actions) and
  `POST /api/admin/rooms/upload` (the room directory file).
- **Module.** `src/privacyfence/web/routes_admin.py`, with `build_routes(...)` for local mode and
  `build_org_routes(...)` for org mode. This mirrors `routes_settings`, one route layer with an auth
  adapter per mode (ADR 0033).
- **Who may use it, local mode.** The routes go through `web/server.py`'s `_owner_only_routes`, as
  Settings does, so only the install owner reaches them.
- **Who may use it, org mode.** A signed-in principal with `is_admin`.
  - No session: a JSON 401 `{"error": "Sign in first."}` for the API, and a redirect to
    `/login?next=/admin` for the page.
  - Signed in but not an admin: a JSON 403 `{"error": "Only administrators can do this."}`, and for
    `GET /admin` an HTML page that says `Only administrators can open this page.`
- **Requests.** CSRF and origin checks reuse each mode's existing helpers, as `routes_settings`
  uses them.
- **Step-up.** Both modes call `routes_settings._apply_step_up_gate` with a new keyword,
  `sensitive: bool | None = None`, passing the action's `sensitive` flag. When it is given, it
  replaces the `action in _SENSITIVE_ACTIONS` test inside `_needs_step_up`, which gains the same
  keyword; existing callers pass nothing and behave as before.
  - `routes_admin` owns its `StepUpChallengeStore`.
  - On a refusal it records the same `Step-up required for <action>, refused` audit entry as
    `_guard_step_up`.
  - Local mode also applies the existing `require_human_session` check to sensitive admin actions,
    as `settings_action` does.
- **Audit.** Every successful change records `routes_settings._record_settings_audit(principal,
  summary)` (decision `settings_change`) inside `with principal_scope(principal):`, as the
  existing org dispatcher does. The summary comes from `admin_config.apply_change`, suffixed with
  ` (admin=<id>)` in org mode and ` (principal=local)` in local mode. An empty summary (a no-op)
  records nothing and saves nothing.
- **Response.** Every `POST /api/admin/...` that succeeds returns the admin page state (below) as
  JSON, which the bridge shim re-renders from. An error returns `{"error": <message>}` with 400
  and is shown the way settings errors are.
- **Getting the bundle and the startup state.**
  - Local mode has no `OrgAuth`. The local builder reads the bundle per request with
    `daemon_main.load_org_config()`, using `{}` on `ConfigurationError`.
  - `startup_admin` (the admin config as loaded at startup) and the startup `org_config` dict
    reach both builders through new keywords on `WebServer.__init__`, `build_app` and
    `_maybe_start_web_server`.
  - The routes mount where the settings routes mount (`web/server.py:1190`, only when a controller
    exists).
- **The nav.**
  - `web_shell` gets `ORG_ADMIN_NAV_ITEMS = ORG_NAV_ITEMS + (("admin", "Admin", "/admin"),)` and
    `def org_nav_items(is_admin: bool) -> tuple[...]`.
  - `wrap`'s `is_org` test becomes `nav_items in (ORG_NAV_ITEMS, ORG_ADMIN_NAV_ITEMS)`.
  - The org pages that know their principal pass `web_shell.org_nav_items(principal.is_admin)`:
    `routes_connect.py:1067`, `routes_approvals.py:505`, `routes_whats_new.py:93` and
    `routes_settings.py:1925` and `:1949`.
  - `/security` (`web/server.py:1389`) keeps the static `ORG_NAV_ITEMS`: its nav is fixed when the
    route is built. This is accepted.
  - Local `_NAV_ITEMS` becomes `(("approvals", "Approvals", "/approvals"), ("settings", "Settings",
    "/settings"), ("admin", "Admin", "/admin"))`.

**The action table.** In `routes_admin.py`:

```python
@dataclass(frozen=True)
class AdminAction:
    modes: frozenset[str]
    sensitive: bool
    applies: Literal["live", "restart", "self"]
```

`applies` says how a change takes effect:

- `"live"`: the dispatcher refreshes the effective config and rebuilds connectors.
- `"restart"`: the change is only saved, and the page shows the restart notice.
- `"self"`: the action already applies itself (the privacy actions, through
  `org_install_policy.apply_change`'s own reload).

It holds exactly these actions. "Both" means `{LOCAL_MODE, ORG_MODE}`. Each action's parameters are
the JSON body keys, with these types: `service: str`, `enabled: bool`, `value: str` and so on.

| Action | Parameters | Modes | Sensitive | Applies |
|---|---|---|---|---|
| `offer_service` | `service` | both | yes (ADR 0070) | live |
| `withdraw_service` | `service` | both | no | live |
| `set_atlassian_assets` | `enabled: bool, workspace_id: str = ""` | both | yes | live |
| `set_server_address` | `service: str` (`grist` or `peek`), `value: str` | both | yes | live |
| `set_unattended_sessions` | `enabled: bool` | both | yes | restart |
| `clear_rooms` | — | both | no | live |
| `set_download_delivery` | `inline_max_bytes: int, link_ttl_seconds: int, allow_disk_staging: bool, agent_links: bool, allow_exports: bool` | org | yes | restart |
| `set_web_push` | `enabled: bool` | org | yes | restart |
| `set_authz` | `allowed_domains: str, groups_claim: str, required_groups: str` (comma- or newline-separated lists) | org | yes | restart |
| `tighten_step_up` | `enabled: bool, require_passkey: bool, scope: str` | org | yes | restart |
| `set_scoped_tokens` | `enabled: bool, max_days: int` | org | yes | restart |
| `set_audit_forwarding` | `enabled: bool, kind: str, syslog_host: str, syslog_port: int, syslog_protocol: str, http_url: str` | org | yes | restart |
| `toggle_pii_detection`, `toggle_pii_category`, `set_default_policy`, `set_category_policy` | as today in `routes_settings._apply_org_action` | org | yes | self |

**Restart labels**, for the `restart_needed` list:

| Section | Label |
|---|---|
| `unattended_sessions` | Unattended sessions |
| `download_delivery` | File delivery |
| `web_push` | Web push |
| `authz` | Sign-in restrictions |
| `step_up` | Step-up |
| `scoped_tokens` | Scoped tokens |
| `audit_forwarding` | Audit forwarding |

**`audit_forwarding.http.bearer_token_env` is not editable on the Admin page.** The daemon sends
the named environment variable's value to the configured URL (`audit_forwarding.py:185-200`). If
the page could set both the name and the URL, an IdP admin could send any secret in the server's
environment to a URL they choose.

- `set_audit_forwarding` keeps the stored name unchanged.
- The page shows it read-only, with: `Set by editing org/admin_config.json on the server.`
- ADR 0241 records this.

- **The four privacy actions** (last row) keep their existing implementation. The
  `_apply_org_action` branches and `_current_pii_flag` move unchanged into `routes_admin`, still
  calling `org_install_policy.apply_change`. `routes_admin.build_org_routes` therefore also takes
  `install_wide_settings` and `install_wide_settings_path`, passed from `web/server.py` as
  `build_org_routes` for settings gets them today.
- **Validation.** `admin_config.apply_change(action, payload)` validates by building the candidate
  section and running it through the existing parser, `org_mode.<X>.from_org_config({"<section>":
  candidate})`. Use `step_up_config.StepUpConfig.from_org_config` and
  `scoped_tokens.policy_from_org_config(..., org_mode=True)` the same way.
  - A `ConfigurationError` becomes `AdminConfigError(str(exc).replace("org_bundle.json's ", ""))`.
  - `set_server_address` validates with `grist_auth.normalize_server_url` or
    `peek_client.normalize_base_url`. Their `GristClientError` or `PeekClientError` becomes
    `AdminConfigError(str(exc))`. An empty value clears the address.
  - `set_scoped_tokens` checks `max_days` itself, because `policy_from_org_config` only logs a bad
    value: it must be an int from 1 to `scoped_tokens.LOCAL_MAX_DAYS` (365). Otherwise it raises
    `Scoped tokens can last from 1 to 365 days.`
  - `step_up.rp_id`, `rp_name` and `batch` have no control on the page. They keep their imported
    values and are set by hand in the file; the docs say so.
  - `set_atlassian_assets` stores `assets` and `assets_workspace_id`.
  - Unknown services raise `Unknown service.`
- **`tighten_step_up`** accepts only changes that make step-up stricter:
  - `enabled` false→true;
  - `require_passkey` false→true;
  - `scope` moving later in `step_up_config.STEP_UP_SCOPES` (`("writes", "writes_and_pii_reads",
    "writes_and_reads")`, narrowest first).
  - A submission that changes nothing is a no-op: an empty summary, nothing saved, nothing
    audited.

  Anything else raises `AdminConfigError("Step-up can only be made stricter here. To relax it,
  edit org/admin_config.json and restart PrivacyFence (ADR 0068).")`.
- **Return value.** `apply_change` returns `(summary, changed_sections)`. Summaries:
  - `Offered <Label>` / `Stopped offering <Label>`
  - `Set Jira Assets to on` / `Set Jira Assets to off`
  - `Set the <Label> server to <value>` / `Cleared the <Label> server`
  - `Turned unattended sessions on` / `Turned unattended sessions off`
  - `Cleared the room directory`
  - `Changed file delivery`
  - `Turned web push on` / `Turned web push off`
  - `Changed sign-in restrictions`
  - `Made step-up stricter`
  - `Changed scoped tokens`
  - `Changed audit forwarding`

  No summary carries a list's contents, a URL's credentials or an environment variable's value.
- **Bodies.** The dispatcher coerces the body like `routes_settings._call_action` does, using the
  same `_coerce` for int, str and bool. A missing parameter gives 400.

**Applying a change.**

- **Live actions.** For an action with `applies == "live"`, after `apply_change` succeeds:
  - Org mode: `oauth_apps.refresh_in_place(org_config, oauth_apps.effective_org_config(bundle,
    admin_config.load(), mode="org"))`, then `connector_registry.evict_all()`, a new method that
    clears `_hosts` and `_last_used` under the lock, then `relay_client.request_check()`.
  - Local mode: the same `refresh_in_place` on the startup `org_config` dict, which
    `_connectors_for_new_principal` closes over (`daemon_main.py:874-876`), so other OS accounts'
    principals see the change too. Then `controller.refresh_connectors()`.
- **Restart-only actions.** These are only saved.
- **Pending restart.** `routes_admin` keeps the admin config as loaded at startup
  (`startup_admin`, passed in by `daemon_main`). The page state's `"restart_needed"` lists the
  labels of the restart-only sections whose stored value differs from `startup_admin`. The page
  shows `Saved. Restart PrivacyFence for these changes to take effect: <labels>.`

**Room directory upload.** `POST /api/admin/rooms/upload` takes a multipart form with one file,
like `org_config_upload` does.

- The file is at most 2 MB and is the JSON `scripts/sync_room_directory.py` now writes:
  `{"rooms": [...], "rooms_synced_at": "<iso>"}`.
- Each room must be an object with a non-empty string `resource_email`. Its other values must be
  strings of at most 500 characters, numbers, booleans or null. There are at most 5000 rooms.
- It is **sensitive**: room names become tool output an agent reads. That means step-up (with
  `routes_settings._step_up_in_force` and a fingerprint of the file's SHA-256, as
  `org_config_upload` does), plus `require_human_session` in local mode. It is CSRF-checked, and
  classified in the route-classification list `routes_admin` keeps, as ADR 0014 requires.
- Summary: `Replaced the room directory (<n> rooms)`.
- The bridge shim intercepts a new action, `upload_rooms`, the way it intercepts
  `install_org_config` (`routes_settings.py:483-521`), and posts the file to this route.

**Bundle upload (local mode).** The upload moves from Settings to the Admin page.

- `routes_settings.org_config_upload`'s body becomes a module-level function
  `handle_org_config_upload(request, *, controller, ..., state_builder)` with unchanged checks:
  CSRF, human session, size, TOFU confirmation (409), then step-up.
- `routes_admin`'s local builder mounts it at `POST /api/admin/org_config/upload`, with
  `state_builder` returning the admin page state.
- `/api/settings/org_config/upload` stays until p8b moves the card off the settings surface;
  p8b then removes it and its `_BESPOKE_SENSITIVE_ROUTE_PATHS` entry.
- The shim's upload paths become `<endpoint>/org_config/upload` and `<endpoint>/rooms/upload`
  once p8a adds the `endpoint` parameter.
- The admin page state carries `"error": controller.error` in local mode, so D3's "are ignored"
  message shows on the Admin page.

**Page state.** `routes_admin` builds:

- `services`: `admin_config.service_rows(bundle, admin, mode=mode)`, one row per `SERVICES` entry
  with these fields:
  - `key`, `label`, `offered`
  - `app_source`: `"bundle"`, `"builtin"` or `"none"`, from `oauth_apps.credentials_source`;
    `"builtin"` for telegram when `telegram_app_credentials()` is not None; `"server"` for grist
    and peek
  - `client_id`: from the bundle or built-in app, `""` otherwise
  - `relay`: `mode == "org" and app_source == "builtin" and key != "telegram"`
  - for grist, `client_id` is the bundle Grist OAuth app's, or `""`
  - `options`: `{"assets": bool, "assets_workspace_id": str}` for atlassian, `{"server_url":
    str, "locked": bool}` for grist (locked when the bundle carries a Grist OAuth app),
    `{"base_url": str}` for peek, `{}` otherwise

  No row carries a secret.
- `rooms`: `{"count": int, "synced_at": str}`.
- `unattended_sessions`, and in org mode `download_delivery`, `web_push`, `authz`, `step_up`,
  `scoped_tokens` and `audit_forwarding`: their stored values, for audit forwarding the variable
  name only.
- `restart_needed`, and in org mode `relay_status` (`relay_client.status()`), `privacy` (the
  install-wide policy rows `_org_state` builds today for admins) and `stats`
  (`org_stats.collect`).

**Rendering.** `settings_window_html.build_html` gains `surface: Literal["settings", "admin"] =
"settings"`.

- `window.__pfCapabilities` gains `surface`.
- `render()` branches first: `if (CAPS.surface === 'admin') { renderAdmin(state); return; }`.
  The admin state never has to carry the settings keys (`connectors`, `telegram_auth`,
  `peek_auth`).
- `renderAdmin` draws its own nav and section switch. It does not call `renderNav` or
  `renderSection`, which read settings-only state. Its section keys and labels, in order:
  - services "Services"
  - features "Features"
  - org_config "Organization configuration"
  - delivery "File delivery"
  - access "Sign-in"
  - step_up "Step-up"
  - tokens "Scoped tokens"
  - audit "Audit forwarding"
  - privacy "Privacy Filter"
  - stats "Stats"

  Each is shown only when the capabilities include it.
- `GET /admin` passes `initial_section="services"`, so the shared fallback
  (`settings_window_html.py:346`, `:1875`) never lands on a hidden section.
- The admin surface gets its own `not_applicable_actions`: the org-only admin actions in local
  mode, and nothing in org mode.

- `_capabilities_for(mode, is_admin, surface)`:
  - The admin surface's sections are `services`, `features` (Jira Assets, server addresses, rooms,
    unattended sessions) and `org_config` (local mode's bundle upload, moved here from Settings).
  - In org mode they are also `delivery`, `access` (authz), `step_up`, `tokens`, `audit`,
    `privacy` and `stats`.
  - The settings surface in org mode no longer has `privacy` or `stats`, for anyone. In local mode
    Settings keeps `privacy` (the owner's own policy) and loses the bundle-upload control.
- The bridge shim posts admin actions to `/api/admin/<action>`. `_settings_bridge_shim` gains an
  `endpoint` parameter, default `/api/settings`.
- **Services status lines:**
  - bundle: `Uses your organization's own app (client <client_id>), from the configuration file.`
  - builtin, local: `Uses the built-in PrivacyFence app.`
  - builtin with relay: `Uses the built-in PrivacyFence app through oauth.privacyfence.eu.`
  - none, offered: `No app is available for this service. Add your organization's own app to the
    configuration file.`
  - grist/peek with no server, offered, org mode: `Set the server address below.`
  - not offered: `Not offered.`
- Each row has a "How to use your own app" link to its provider setup doc on privacyfence.eu
  (`https://privacyfence.eu/docs/<doc-slug>/`, where the slug is the doc file name without `.md`:
  google-cloud-setup, slack-setup, salesforce-setup, atlassian-setup, miro-setup, grist-setup,
  peek-setup, telegram-setup). Rows with app_source `"bundle"` or `"builtin"` also show the line
  `Changing the app makes everyone who connected <Label> connect again.`
- In org mode the relay status line above the services:
  - `Relay registration: registered <time>.`
  - `Relay registration failed: <detail>. Built-in apps cannot sign in until it succeeds.`
  - `Relay registration: not needed.`
- Every form on the page posts its whole section; a form's fields mirror its action's parameters.

### D7. Bundle tooling

- **`scripts/build_org_bundle.py`.** These flags write a legacy key. They are hidden with
  `help=argparse.SUPPRESS` and stay parseable only to refuse:
  - `--enable-telegram`, `--enable-peek`, `--enable-grist` and `--peek-base-url`;
  - `--grist-server-url` when given without `--grist-client-id`;
  - `--atlassian-assets` and `--atlassian-assets-workspace-id`;
  - `--authz-allowed-domain`, `--authz-groups-claim` and `--authz-required-group`;
  - `--step-up-enabled`, `--step-up-disabled`, `--step-up-scope`, `--step-up-rp-id`,
    `--step-up-rp-name`, `--step-up-require-passkey` and `--step-up-no-require-passkey`;
  - `--enable-unattended-sessions` and `--disable-unattended-sessions`;
  - `--enable-scoped-tokens`, `--disable-scoped-tokens` and `--scoped-token-max-days`;
  - `--downloads-inline-max-bytes`, `--downloads-link-ttl-seconds`, `--downloads-disable-staging`,
    `--agent-links`/`--no-agent-links` and `--exports`/`--no-exports`;
  - `--web-push`/`--no-web-push` (one `BooleanOptionalAction`);
  - `--enable-audit-forwarding`, `--disable-audit-forwarding` and every
    `--audit-forwarding-*` option.

  When any of them is given, the script prints `error: <flag> has moved to the Admin page (ADR
  0241); remove it.` for each one and exits 2 without writing.

  `--idp-step-up-acr-value` writes `idp.step_up_acr_values`, which stays: it is part of the IdP, so
  it is **not** refused.

  Also update:
  - the "nothing to write" check (`build_org_bundle.py:902-910`), which names unattended sessions
    and scoped tokens, so it names only what remains;
  - `--mode local`'s popping of operational keys (`:770-776`), which now has nothing to pop;
  - `tests/unit/test_docs_configuration_reference.py:128-135`: `_bundle_options()` skips actions
    whose `help is argparse.SUPPRESS`, so the reference no longer has to list refused flags.
- `--merge` drops legacy keys from the merged bundle and prints `note: dropped <names>; they are
  managed on the Admin page now.`
- The script's own copy of the canonicalization and signing logic is unchanged (ADR 0016).
- **`scripts/sync_room_directory.py`.** It now writes a standalone file: `--output rooms.json`
  replaces `--org-config`, and so do the bundle-merge, signature and `--sign-key` logic. The file
  is `{"rooms": [...], "rooms_synced_at": "<iso>"}`, mode 0600. The administrator uploads it on
  the Admin page. Passing `--org-config` prints `error: --org-config has been replaced by --output;
  upload the file on the Admin page (ADR 0241).` and exits 2.

### D8. Test isolation

In a source checkout `paths.data_dir()` is the repository root. A test that forgets to patch it
would write `org/admin_config.json` or `org/relay_token.txt` into the checkout. A developer with a
generated `_builtin_apps.py` or `PRIVACYFENCE_BUILTIN_APPS` set would also see unrelated tests
change.

p2 adds an autouse fixture, `_isolate_admin_config_and_builtin_apps`, to `tests/conftest.py`,
modelled on `_isolate_whats_new_state` (a lazy `tmp_path` through `request.getfixturevalue`). It
patches:

- `admin_config.admin_config_path` to `tmp_path / "admin_config.json"`;
- `app_credentials.builtin_app` to return `None`.

p5 extends it to patch `relay_client.relay_token_path` (a function returning
`paths.org_dir() / "relay_token.txt"`) to `tmp_path / "relay_token.txt"`.

**Exempt modules.** The fixture does nothing when `request.module.__name__` ends with
`test_app_credentials`, `test_admin_config` or `test_relay_client`. Those modules test the real
functions and patch `paths.data_dir` to `tmp_path` themselves, so the real path functions run and
count toward their 100% coverage. Any other test that needs built-in apps patches `builtin_app`
itself.

### D9. Mode and server from the environment, and the Docker image starts in org mode

**`mode` may come from the environment.** `org_mode.resolve_mode(org_config)` decides the mode in
this order:

1. When `org_config` has `"mode"`, that value, validated as today.
2. Otherwise the environment variable `PRIVACYFENCE_MODE`, which must be `local` or `org`. Any other
   value raises `ConfigurationError("PRIVACYFENCE_MODE must be \"local\" or \"org\", got <v>")`.
3. Otherwise `"local"`.

When both the bundle and the variable are set and they differ, it raises
`ConfigurationError("org_config.json says \"mode\": \"<a>\" but PRIVACYFENCE_MODE says \"<b>\"; remove one of them")`.

Because `load_org_config`'s "org mode needs a signed bundle" check calls `resolve_mode`, a bundle
without `mode` under `PRIVACYFENCE_MODE=org` must still be signed.

**`server` may come from the environment.** `ServerConfig.from_org_config` fills each value the
bundle's `server` section does not set:

- `bind_host` from `PRIVACYFENCE_BIND_HOST`;
- `port` from `PRIVACYFENCE_PORT` (an integer from 1 to 65535);
- `issuer_url` from `PRIVACYFENCE_ISSUER_URL`;
- `trusted_proxies` from `PRIVACYFENCE_TRUSTED_PROXIES`, a comma-separated list. Behind the
  shipped Caddy, that is `172.30.87.2`, the value `docs/org-mode-docker.md:135` today passes as
  `--server-trusted-proxy`. Without it a bundle-free server behind Caddy would ignore
  `X-Forwarded-*`.

The bundle always wins over the environment. In single-user mode (below) a missing `issuer_url` defaults
to `http://localhost:<port>`.

**Single-user mode.**

- `org_mode.is_single_user(org_config) -> bool` returns `resolve_mode(org_config) == "org" and not
  org_config.get("idp")`.
- In single-user mode, `_start_org_web_server` no longer raises "org mode requires an \"idp\" section".
  Instead it requires the issuer URL's scheme to be `http` and its hostname to be `localhost` or
  `127.0.0.1`. Otherwise it raises `ConfigurationError("Organization mode without an identity
  provider is single-user mode and runs only at http://localhost or http://127.0.0.1. Add an \"idp\"
  section to org_bundle.json to serve any other address.")`.
- Single-user mode needs no bundle at all. Built-in apps and the admin config supply everything else.
- **A real server never falls back to single-user mode.** Single-user mode is refused when the data directory
  shows it belonged to an organization server. That means a pinned signing key exists under
  either name (`org_bundle_signing_pubkey.txt` or the 6.x `org_config_signing_pubkey.txt`, D13),
  or the admin config's `org_server` flag is set (D14), or the admin config's
  `imported_from_bundle_sha256` is non-empty. The message is `ConfigurationError("This data
  directory belonged to an organization server; restore org_bundle.json, or use a new data
  directory for single-user mode.")`. Without this, deleting the bundle would silently turn a production
  server into single-user mode reachable on `0.0.0.0` with only a setup code (the downgrade
  `load_org_config`'s docstring warns about).
- **Types.** `OrgAuth.idp` becomes `IdpConfig | None` (`web/server.py:698`), and
  `OrgOAuthProvider.__init__`'s `idp` becomes `IdpConfig | None` (`oauth_provider.py:251`), with
  `authorize` guarded. `routes_org_identity` and `routes_org_stepup` are mounted only when `idp` is
  not None (`web/server.py:1349`, `:1373`), and the issuer log line (`daemon_main.py:1253-1255`)
  logs `single-user` instead when it is None.
- At every single-user start, `daemon_main` logs at WARNING `Single-user mode: one administrator, no
  identity provider, on this computer only.`

**The Docker image.**

- `deploy/docker/Dockerfile` adds `ENV PRIVACYFENCE_MODE=org PRIVACYFENCE_BIND_HOST=0.0.0.0` to
  its existing `ENV` block.
- `deploy/docker/healthcheck.py` reads the bundle's `server.port`. With no bundle, or no port in
  it, it uses `PRIVACYFENCE_PORT`, else 8765, so a single-user container reports healthy.
- `tests/integration/test_org_docker_image.py::test_refuses_to_start_without_a_bundle` (`:185`,
  asserting exit 78) is replaced by the single-user start test.
- `deploy/docker/docker-entrypoint.sh` no longer exits 78 when there is no bundle. Without one, it
  prints `No organization bundle: starting in single-user mode at http://localhost:8765 (docs/org-mode-docker.md, "Run it on this computer").`
  to stderr, then `exec`s as before.
- The single-user command, documented in `docs/org-mode-docker.md`, is
  `docker run --rm -p 127.0.0.1:8765:8765 -v pf-data:/var/lib/privacyfence-org/.privacyfence ghcr.io/privacyfence/privacyfence-org:latest`.
  The `127.0.0.1:` publish keeps it unreachable from the network. The image cannot check
  this, and the docs say so plainly. Single-user mode is opened at `http://localhost:8765`, not
  `127.0.0.1`: the Host allowlist and the passkey's rp_id are both `localhost`.
- `tests/integration/test_org_docker_image.py` covers the image. A phase that changes the image
  dispatches `build.yml` (in `ci.dispatchable`) to run it, because the container cannot build
  images.

**The relay on localhost.** This amends the D5 contract.

- A loopback host is `localhost` or `127.0.0.1` with an optional port. It needs no registration and
  no domain proof.
- **Worker:** `GET /callback/:grant` with a state whose host part is loopback returns a 302 to
  `http://<host>/oauth/callback/<grant>?<query>` without consulting the registry. The browser
  performs that redirect, so the code reaches only the machine the person is using, the same trust
  as a desktop install's loopback redirect.
- **Worker:** `POST /register` with a loopback host still answers 400 `host_not_public`.
- **`relay_client`:**
  - `register` refuses a loopback issuer without calling the Worker. Its "https with no path"
    check is relaxed to "https with no path, or http on a loopback host".
  - `_tick` treats a loopback issuer as not needing registration.
  - `routes_connect.start` skips the "registration has not succeeded" refusal for a loopback
    issuer.
  - The Admin page's relay status line says `Relay registration: not needed on localhost.`

### D10. Single-user sign-in: a setup code, then a passkey

**Module `src/privacyfence/web/single_user_auth.py`.** Its `build_routes(...)` is mounted instead of
`routes_org_identity.build_routes` when `org_mode.is_single_user(org_config)`.

- **The principal.** `SINGLE_USER_PRINCIPAL = Principal(id="single-user", email="admin@localhost",
  display_name="Administrator", is_admin=True)`.
- **The setup code.** It is generated and logged inside `build_routes`, so single_user_auth's own tests
  cover it.
  - It is made only when `webauthn_stepup.has_credentials(SINGLE_USER_PRINCIPAL)` is false: eight
    characters from `secrets.token_hex(4).upper()`, shown as `XXXX-XXXX`, kept in memory only.
  - It is logged at WARNING: `Setup code: <code>. Open http://localhost:<port>/login to set
    up your passkey.`
  - **Checked on every request.** Every request checks `has_credentials` again, so the code works
    only while no passkey is enrolled, and a later recovery wipe does not bring an old code back
    to life.
  - **Comparing.** The typed value is normalized: stripped, upper-cased, with a dash inserted
    after four characters when it is missing. Anything that is not then `^[0-9A-F]{4}-[0-9A-F]{4}$`
    counts as a wrong attempt. The comparison is `hmac.compare_digest(typed.encode(),
    code.encode())`.
  - **Limits.** Five wrong attempts discard the code until the next start, logging `Setup
    code used up after 5 wrong attempts; restart PrivacyFence for a new one.` A successful use
    also discards it; a restart issues a new one while no passkey is enrolled.
- **Cookies.** `pf_setup_binding` and the session cookie are `Secure` only when the issuer is
  `https`. `org_session.set_session_cookie` gains `secure: bool = True`, and single_user_auth passes
  `False` for an `http` issuer. Safari does not keep `Secure` cookies on `http://localhost`.
- **`GET /login?next=<path>`:**
  - With no passkey enrolled, a form: "Setup code" input, button "Continue".
  - Otherwise, a "Sign in with your passkey" button that runs the WebAuthn get ceremony.
  - `next` is cleaned with `routes_org_identity._safe_next_path`.
  - The page is rendered with `web_shell.plain_page(..., nonce=nonce_for(request))`, as
    `routes_org_identity`'s `signed_out` page is.
  - It sets `pf_setup_binding`: a random value, HttpOnly, SameSite=Strict, 10-minute max-age,
    echoed as a hidden field.
- **`POST /login/setup`** (form: `code`, `next`, the hidden binding value):
  - It is origin-checked with `org_session.check_origin`.
  - The cookie must match the hidden field (`hmac.compare_digest`).
  - On success it consumes the code, creates an org session for `SINGLE_USER_PRINCIPAL` with
    `org_session`'s existing creation and cookie helpers (as `routes_org_identity.login_callback`
    does, including issuing a fresh session id), and redirects to `/security?next=<safe next>`.
  - On failure it re-renders the form with `That code is not right.`, or after the fifth failure
    `The setup code is used up. Restart PrivacyFence to get a new one.`
- **`routes_security`.** After a successful enrollment, when the page was opened with a `next`
  that `_safe_next_path` accepts, it shows a "Continue" link to it. That carries an MCP client's
  `/oauth/single-user/continue` through first-time setup.
- **`POST /login/passkey/options` and `POST /login/passkey/verify`:**
  - They use `webauthn_stepup.begin_assertion` and `verify_assertion` for `SINGLE_USER_PRINCIPAL`.
  - The `rp_id` and origin are the ones `/security` enrolled with: `StepUpConfig.rp_id` (the
    `step_up` config's, defaulting to the issuer host, `routes_security.py:516`, `:545`) and
    `issuer_url.rstrip('/')`.
  - They are bound with the same cookie pattern. The challenge lives in a single-use store keyed
    by that cookie.
  - On success they create the session (a fresh session id) and return `{"redirect": next}`.
- **`POST /logout` and `GET /signed-out`** behave exactly as `routes_org_identity`'s, including
  `end_browser_push`. The nav's sign-out form posts to `/logout` (`web_shell.py:534-546`).
- **MCP clients through the OAuth server.**
  - `OrgOAuthProvider` gets `single_user: bool`, with `idp` `None` when `single_user` is true.
  - `authorize` records the pending authorization as today but returns
    `/login?next=/oauth/single-user/continue%3Fstate%3D<own_state>`.
  - `routes_mcp.mount_org_oauth(provider, *, issuer_url, sessions: OrgSessionStore | None = None)`:
    - It mounts `GET /oauth/single-user/continue?state=` only when `provider.single_user`, and the IdP
      callback route (`IDP_CALLBACK_PATH`, `routes_mcp.py:1091`) only when it is not.
    - The call at `web/server.py:1335` passes `sessions=org.sessions`.
  - The continue route needs a session for `SINGLE_USER_PRINCIPAL`; without one it redirects to
    `/login?next=<itself>`. It calls `provider.handle_single_user_continue(state=..., principal=...)`.
  - `handle_idp_callback` is split into `_pop_pending(state)`, the IdP-specific claims step, and
    `_finish_for_principal(pending, principal) -> str` (today's consent check and code minting,
    unchanged). `handle_single_user_continue` is `_pop_pending` + `_finish_for_principal`, so consent
    (ADR 0177) applies to single-user clients unchanged.
- **Step-up and approvals** work as on any org server; `SINGLE_USER_PRINCIPAL`'s passkey is the step-up
  passkey.
- **Lost passkey.** Stop the container and delete the file
  `webauthn_stepup._credentials_path(SINGLE_USER_PRINCIPAL)` resolves to in the volume. The next start
  prints a new code. This goes in the docs.
- **Audit.** Sign-in outcomes are audit-logged the way `routes_security._audit`
  (`routes_security.py:452`) writes them: an `AuditEntry` under `principal_scope`. The decisions
  are `single_user_setup_signin`, `single_user_setup_refused`, `single_user_passkey_signin` and
  `single_user_passkey_refused`. A refusal never records the typed code.

### D11. Local mode is deprecated in 7.x

- **The text.** One constant, `org_mode.LOCAL_MODE_DEPRECATION`: `Desktop mode is deprecated and
  will be removed in PrivacyFence 8. Move to an organization server, or try the Docker image on
  this computer: https://privacyfence.eu/docs/moving-to-organization-mode/`
- **Daemon log.** `run_app` logs it at WARNING at every local-mode start.
- **`privacyfence_status`.** `mcp_dispatch`'s status result, where `result` is built
  (`web/mcp_dispatch.py:506-508`) so all three return paths carry it, gains
  `"deprecation": LOCAL_MODE_DEPRECATION` in local mode and `null` in org mode. The tool
  description in `web/mcp_tools.py:257` adds "deprecation (local mode: a notice to relay to the
  human once)". Regenerate `docs/tools-reference.md` with `scripts/generate_tools_reference.py` if
  a test pins it.
- **Web banner.** `web_shell.wrap` gains a keyword `deprecation_html: str = ""`.
  - When it is non-empty, the page shows its own strip, `<div class="pf-shell-deprecation"
    role="note">…</div>`, above `banner_html`. The strip is not dismissible and is styled like the
    existing notice strip.
  - Every local-mode page passes the text as a link. These are the `wrap` call sites whose
    `nav_items` is the local `_NAV_ITEMS`; grep `web_shell.wrap(` and the local routes in
    `routes_settings`, `routes_approvals`, `routes_security` and `routes_admin`.
  - Org pages pass nothing.
- **Companion.** Once per OS user, when the tray (`companion._run_tray`) or `--serve` starts after
  the upgrade, the companion shows the text through `icon.notify(...)`, or `_notify_send` under
  `--serve`.
  - The "shown" marker lives at a stdlib-computed per-user path,
    `companion._notice_state_path()`:
    - macOS: `~/Library/Application Support/PrivacyFence Companion/notices.json`
    - Windows: `%APPDATA%\PrivacyFence Companion\notices.json`
    - Linux: `${XDG_STATE_HOME:-~/.local/state}/privacyfence-companion/notices.json`
  - The marker is the JSON `{"local_mode_deprecated_7": true}`, written atomically. A failure to
    read or write it means the notice shows again next time and is never an error.
- **Docs.** p13b adds `docs/moving-to-organization-mode.md` (moving to a server or the Docker image,
  bringing rules along per the existing "Moving a person from a desktop install" section) and marks
  the install guides as deprecated.
- **Changelog.** The changelog says so under a breaking-changes note.
- **Release.** The release itself is cut as 7.0.0 by the maintainer after merge, per
  `docs/releasing.md`. No phase tags anything.

### D12. 7.x is the migration checkpoint for 8.x

8.x is meant to delete the code that upgrades data from older releases:

- the v1-policy clean-up (ADR 0047);
- the v2→v3 rule conversion (ADR 0172);
- tokens that only name rules (ADR 0236), with the limit-pair successors (ADR 0228);
- the legacy verb aliases (ADR 0171);
- this plan's import of operational settings from the bundle (D3).

That is safe only if every install has run a 7.x release that finished all of them, for every
person, and left proof. Today there is no proof: no install-wide data-format marker exists. And in
org mode the v1 clean-up runs only when a person's settings are first loaded, so someone who never
signs in during 7.x would keep old data.

**The marker.**

- The file is `paths.data_dir() / "data_format.json"`, returned by
  `data_format.marker_path()` at call time.
- It is written atomically, mode 0600, as
  `{"format": 7, "written_by": "<privacyfence.__version__>", "completed_at": "<UTC ISO-8601>"}`.
- `format` is an integer. It is the contract 8.x reads, so it never changes shape: 8.x and its
  installers rely on `format` alone.
- `data_dir()` is the system root on a separated install, `~/.privacyfence` or
  `%LOCALAPPDATA%\PrivacyFence` otherwise, and the data volume in Docker. So the marker travels
  with the data, including in backups.

**Module `src/privacyfence/data_format.py`.**

- `DATA_FORMAT = 7`.
- `marker_path() -> Path`.
- `read_marker() -> dict[str, Any] | None`. A missing file gives `None`. An unreadable or
  malformed file, or one with a non-integer `format`, logs a warning and gives `None`, so the next
  successful pass rewrites it.
- `check_not_newer(marker) -> None`. When `marker["format"] > DATA_FORMAT` it raises
  `org_mode.ConfigurationError("This data was last used by PrivacyFence <written_by> (data format <n>). This release, <__version__>, cannot read it. Install <written_by> or newer.")`.
  - `daemon_main.main()` calls it right after `check_runtime_identity` (about
    `daemon_main.py:2487`), before anything reads or rewrites data, including
    `load_config(args.config, principal=LOCAL_PRINCIPAL)` at `:2510`, the CLI OAuth flags and
    `--revoke-scoped-tokens`.
  - A `ConfigurationError` there goes to the existing `_refuse_to_start` path.
- `migrate_everything(*, log: Callable[[str], None]) -> list[str]` returns the problems it found,
  empty when everything succeeded. It never raises for a single principal's data: every
  `OSError`, `yaml.YAMLError` or `json.JSONDecodeError` on one file becomes a problem naming the
  principal and the file.
  - **When.** It runs at every 7.x start, in both modes, in `run_app` after `load_admin_config` and
    before the web server is built.
  - **Logging.** `run_app` passes `log=logger.warning`, so problems are logged at once rather than
    deferred.
  - **What it replaces.** It replaces `_upgrade_all_principals`, which already covers the org-mode
    part of steps 2 and 3.
  - **For whom.** Every principal in `web.mcp_auth.scoped_token_principals()`: the local principal
    plus every `users/<id>/`, which in local mode includes other OS accounts' `os-…` principals.
    Each runs under `principal_scope`:
  1. **Settings file.** Resolve `authority/config/settings.yaml` with
     `daemon_main._resolve_authority_path`. Import it inside the function, because daemon_main
     imports data_format.
     - A missing file means no rules: go to step 3 with `{}`.
     - A file that does not parse to a mapping is the problem `"<principal id>: settings.yaml is
       not a mapping"`.
  2. **v1 clean-up and rules.**
     - Call `policy_store.drop_converted_v1_sections(config)`. When it removed anything, write the
       file with `atomic_write_text`. A failed write is the problem `"<principal id>: could not
       rewrite settings.yaml"`, and the rest is skipped for that principal.
     - Then `policy_store.reject_v1_sections`. A `V1PolicyConfigError` is the problem
       `"<principal id>: settings.yaml still has v1 policy sections"`, and the rest is skipped.
     - Then `policy_upgrade.ensure_scope_rows`. Afterwards **re-read the file**. If
       `policy_upgrade._v2_rules` of its `auto_accept` section is not `None`, the problem is
       `"<principal id>: rules are still in the version-2 format"`. This catches the case where
       `ensure_scope_rows` returns `True` after a failed write (`policy_upgrade.py:86-90`).
  3. **Tokens.**
     - Run `policy_upgrade.ensure_token_rules(config, principal, log=log)`.
     - Then read `authority_dir(principal) / "scoped_tokens.json"` directly. An unreadable file is
       the problem `"<principal id>: scoped_tokens.json cannot be read"`.
     - Every entry that has a `rule_ids` key and no `rules` key adds the problem `"<principal id>:
       scoped token <id> still names rules"`.
  4. **Verb names.** Run `policy_upgrade.ensure_canonical_verbs(config, resolved, principal) ->
     list[str]`, which returns problems. It rewrites **only the `verbs` value** of rows whose
     `verbs` list contains a key of `policy.registry.LEGACY_VERB_ALIASES`. Every other key, and
     every other row, is kept byte-for-byte in meaning; nothing is re-serialized.
     - **In settings.yaml.** Only a version-3 `auto_accept` section is touched. For each row dict
       in `rules`:
       - if its `verbs` names a legacy alias, set `verbs` to the sorted, de-duplicated list of
         canonical names (each alias mapped through `LEGACY_VERB_ALIASES`, other names kept);
       - first check the row is readable with `scope_rows.row_from_dict(row) is not None`. An
         unreadable row is left as it is and is the problem `"<principal id>: rule <id or 'without
         an id'> could not be read"`.
       - When anything changed, back up once to `<file>.pre-canonical-verbs.bak` (never
         overwriting an existing backup), then write with `atomic_write_text`. A failed backup or
         write is the problem `"<principal id>: could not rewrite settings.yaml"`.
     - **In scoped_tokens.json.** Use a new raw-file function next to `own_rules_in_file`:
       `scoped_tokens.canonicalize_verbs_in_file(path) -> tuple[bool, list[str]]`.
       - It loads the JSON, and for each entry's `rules` list edits only `verbs` as above.
       - It leaves legacy entries (`rule_ids`), unknown keys and unreadable rows untouched.
       - It writes with `atomic_write_json` only when something changed.
       - It returns `(changed, problems)`.
       - It does not use the store's `_write`/`_to_json`, which re-serialize and drop what they
         cannot read. No store exists yet at this point anyway: `set_store` is called later, in
         `web/server.py:1562`.
  - **Install-wide.** The admin config must exist (`admin_config.load()` is not `None`, which it is
    after `load_admin_config`). Otherwise the problem is `"the admin config has not been created"`.
  - **The marker.** When the list is empty and `read_marker()` is `None` or has `format < 7`, write
    the marker. A marker already at format 7 is left as it is. Each problem is logged at WARNING,
    prefixed `Not ready for PrivacyFence 8: `, and the marker is not written. The next start tries
    again.
  - **Out of scope.**
    - A local `--config` path that points outside the authority directory is not walked. The
      marker covers the data directory only, and 8.x drops local mode anyway.
    - `former_ids` on rows (ADR 0172) stays, because it is data, not a format.
    - The audit log's chainless lines (ADR 0176) are history, not something to convert. 8.x keeps
      reading them or decides otherwise in its own plan.
    - Serial-less bundles (ADR 0200) are 8.x's decision.
- **Readiness on the Admin page.** `data_format.status() -> dict` returns `{"ready": bool,
  "problems": list[str]}` from the last `migrate_everything` run, kept in module state; it is reset
  by `data_format.reset_for_tests()` in `tests/conftest.py::_reset`. The admin page state carries
  it as `"upgrade_ready"`. The Admin page shows, at the bottom:
  - `Ready for PrivacyFence 8: yes.`, or
  - `Ready for PrivacyFence 8: not yet. <n> problem(s): <first three problems>. Fix them and restart; see the upgrade notes.`
- **Test isolation.** The D8 conftest fixture also patches `data_format.marker_path` to
  `tmp_path / "data_format.json"`. `test_data_format` is an exempt module.

**What 8.x does with it.** This is recorded in ADR 0246 and is not built here.

- At startup, if the data directory holds existing data (`settings.yaml`, `credentials/`, `users/`
  or `org/`) and the marker is missing or has `format < 7`, 8.x refuses to start with: `This data
  comes from a PrivacyFence release older than 7. Install PrivacyFence 7.x, start it once so it can
  upgrade your data, then install this release.`
- A data directory with no existing data is a fresh install, and 8.x writes `format` 8.
- 8.x's installers (deb `preinst`, the macOS pkg `preinstall`, the Windows installer) may read the
  same file before replacing anything, so an upgrade stops before the old release is removed.
  Because of that, the marker's path and its `format` key are a stable contract.

### D13. The 6 → 7 upgrade is reversible

A 7.x install must be able to go back to the latest 6.x release (6.9.x) and keep running with the
`org_config.json` it had. So:

- 7.x never writes, renames or deletes a 6.x bundle or pin file.
- Every other file 6.x also reads stays in a format 6.9.x reads. That covers settings.yaml,
  scoped_tokens.json, `org/oauth_clients.json`, `oauth_refresh.json`, the VAPID key and
  `credentials/`.

**New file names, adopted by copying.**

| What | 6.x name (left untouched) | 7.x name |
|---|---|---|
| The signed bundle | `org/org_config.json` | `org/org_bundle.json` |
| The pinned signing key | `org/org_config_signing_pubkey.txt` | `org/org_bundle_signing_pubkey.txt` |
| The pinned serial | `org/org_config_serial.txt` | `org/org_bundle_serial.txt` |

The pin files move too. If 7.x installed a newer bundle and advanced a shared serial pin, 6.x
would refuse its own older `org_config.json` after a downgrade (ADR 0200).

- **Constants.**
  - `org_bundle_signing.py`: `PINNED_PUBKEY_FILENAME = "org_bundle_signing_pubkey.txt"` and
    `PINNED_SERIAL_FILENAME = "org_bundle_serial.txt"`, plus
    `LEGACY_PINNED_PUBKEY_FILENAME = "org_config_signing_pubkey.txt"` and
    `LEGACY_PINNED_SERIAL_FILENAME = "org_config_serial.txt"`.
  - `paths.py`: `ORG_BUNDLE_FILENAME = "org_bundle.json"`,
    `LEGACY_ORG_CONFIG_FILENAME = "org_config.json"` and `org_bundle_path() -> Path`, which returns
    `org_dir() / ORG_BUNDLE_FILENAME`.
- **Adoption.** `org_bundle_signing.adopt_legacy_files(org_dir: Path) -> bool` is called at the
  top of `daemon_main.load_org_config()`, so it covers the daemon, the CLI and every script that
  loads the bundle through it.
  - When `org_bundle.json` does not exist and `org_config.json` does:
    - copy the legacy file's bytes unchanged to `org_bundle.json` (`atomic_write_bytes`, mode
      0600), so the signature still verifies;
    - copy each legacy pin file to its new name when the new one does not exist (see the next
      bullet, and D15 SEC-12, which limits pin copying to before `pins_adopted` is recorded);
    - write `org/org_bundle_adopted.json` = `{"from": "org_config.json", "sha256": "<hex of the
      copied bytes>", "at": "<UTC ISO-8601>"}`;
    - log at WARNING `Copied org/org_config.json to org/org_bundle.json for PrivacyFence 7. The
      original is kept unchanged so that you can go back to 6.x.`;
    - return True.
  - Independently of the bundle, until `org_bundle_adopted.json` records `"pins_adopted": true`
    (D15 SEC-12): each legacy pin file whose new-name copy is
    missing is copied. A 6.x data directory whose bundle was removed but whose pins remain keeps
    its key and serial protection, and the next upload cannot trust a new key on first use.
  - Otherwise it does nothing to the bundle and returns False.
  - It never modifies or deletes a legacy file. A failed copy raises `ConfigurationError` naming
    the file, so the daemon refuses to start rather than run with half a configuration.
  - `load_org_config` sets the module-level `daemon_main._bundle_adopted_this_start: bool` only
    ever to True (`|=`), because `load_org_config` runs several times per process.
    `tests/conftest.py::_reset` sets it back to False. When it is True, `run_app` records an audit entry `org_bundle_adopted` right after
    `log_org_config_bundle_hash`.
- **After adoption** 7.x reads and writes only the new names.
  - When `org_config.json` still exists and its SHA-256 differs from the one recorded in
    `org_bundle_adopted.json`, every start logs at WARNING `org/org_config.json changed after the
    upgrade and is ignored: PrivacyFence 7 reads org/org_bundle.json. Install the new bundle on
    the Admin page, or copy it to org/org_bundle.json.`
  - That covers an operator who followed 6.x docs and copied a new bundle to the old name.
- **Every 7.x reader and writer uses `paths.org_bundle_path()`:**
  - `daemon_main.load_org_config` (`:485`) and `log_org_config_bundle_hash` (`:548`);
  - `settings_controller.install_org_config_bytes` (`:1145`) and the snapshot's
    installed-date read (`:2108`). When `org_bundle_adopted.json` exists and the bundle's SHA-256
    still equals its `sha256`, the snapshot shows the legacy file's mtime as the installed date,
    not the copy time;
  - `deploy/docker/healthcheck.py:16` reads the new name and falls back to the old one.
    `deploy/docker/docker-entrypoint.sh:5` counts either file as "a bundle is installed", because
    adoption runs in the daemon. It keeps its message prefix `No organization bundle at`, which
    `tests/integration/test_org_docker_image.py:188` asserts; p15 replaces that test;
  - `scripts/build_org_bundle.py`'s `--output` default becomes `org_bundle.json` (`:239`);
  - `scripts/qa_authenticate_connectors.py:61`: the default path constant changes to the new
    name. The backup name at `:119` uses `DEFAULT_ORG_CONFIG_PATH.name` instead of a hard-coded
    `org_config.json`, and the docstring (`:24`, `:41`, `:107`) names the new file;
  - user-facing messages that name `org_config.json` as the file to edit. Those are in
    `daemon_main.load_org_config`, about 25 in `org_mode.py` (`:47-512`), `step_up_config.py:293`
    and `:299`, and the D9/D10 messages in this plan. They say `org_bundle.json`. p6's mapping of
    parser errors strips `"org_bundle.json's "` accordingly. `web/mcp_dispatch.py:653` and
    `sync/src/privacyfence_sync/runner.py:41` are rewritten by p18.
- **The other 7.x writes are 6.x-compatible.**
  - `admin_config.json`, `data_format.json`, `relay_token.txt` and `org_bundle_adopted.json` are
    new files that 6.x ignores.
  - The settings.yaml and scoped_tokens.json rewrites D12 performs produce formats the latest 6.x
    already reads: version-3 rows, canonical verb names, tokens holding their own rules. 6.9.x is
    the current code base, where all three exist.
  - What does not carry back to 6.x: changes made on the Admin page, bundles installed under 7.x,
    connections made through the relay (p5), and passkeys or connections made in single-user
    mode. A downgrade returns to the configuration
    `org_config.json` holds.
- **Re-upgrading after a downgrade.** `org_bundle.json` and `admin_config.json` still exist, so
  7.x uses them as they were. To start from 6.x's current files instead, delete
  `org/org_bundle.json`, `org/org_bundle_adopted.json`, the two new pin files and
  `org/admin_config.json`, then start 7.x. Adoption and the import run again. The docs give
  exactly this list.
  - Deleting only `org_bundle.json` on a 7.x server re-adopts the old `org_config.json`. If 7.x
    has since recorded a newer serial, the new serial pin refuses that older bundle with its
    existing "delete the serial pin" message. The docs say this next to the re-upgrade list.
- **Single-user safety (D9)** treats either pin-file name as "this belonged to an organization server".
- **8.x (ADR 0246's list).** 8.x deletes `adopt_legacy_files` and the legacy constants along with
  the other pre-7 converters. It keeps the "org_config.json is present and ignored" warning, so
  an operator who copies a bundle there out of 6.x habit is not met with silence. The D12 checkpoint does not need the legacy file, because the marker
  is written only after adoption has run.

### D14. An organization server can start from environment variables alone

With built-in apps and the relay, the IdP is the only app an operator still registers. The signed
bundle then carries little besides the IdP and the server address, both of which can come from the
environment. A multi-user server's bootstrap becomes:

1. Point DNS at the host.
2. Register one OIDC app with the IdP, with the three redirect URIs.
3. Write `privacyfence.env`.
4. Run `docker compose up -d`.
5. Sign in as an admin and offer services.

**The IdP from the environment.** `org_mode.idp_section(org_config) -> dict[str, Any] | None`
returns the `idp` section `IdpConfig.from_org_config` reads.

- When `org_config` has a non-empty `idp`, that section, unchanged: a bundle wins. If any
  `PRIVACYFENCE_IDP_*` variable or `PRIVACYFENCE_ADMIN_EMAILS` is also set, log at WARNING once:
  `PRIVACYFENCE_IDP_* and PRIVACYFENCE_ADMIN_EMAILS are ignored: org_bundle.json has an "idp"
  section.` The once-only flag is a module-level variable that `tests/conftest.py::_reset` clears.
- Otherwise, when `PRIVACYFENCE_IDP_ISSUER` and `PRIVACYFENCE_IDP_CLIENT_ID` are both set, a
  section built from the environment:

  | Variable | Section key |
  |---|---|
  | `PRIVACYFENCE_IDP_ISSUER` | `issuer` |
  | `PRIVACYFENCE_IDP_CLIENT_ID` | `client_id` |
  | `PRIVACYFENCE_IDP_CLIENT_SECRET`, or the contents of the file named by `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE` (stripped of a trailing newline) | `client_secret` |
  | `PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM` | `admin_group_claim` |
  | `PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES` (comma-separated) | `admin_group_values` |
  | `PRIVACYFENCE_IDP_STEP_UP_ACR_VALUES` (comma-separated) | `step_up_acr_values` |
  | `PRIVACYFENCE_ADMIN_EMAILS` (comma-separated) | `admin_emails` |

  - Setting both `PRIVACYFENCE_IDP_CLIENT_SECRET` and `..._FILE` raises
    `ConfigurationError("Set PRIVACYFENCE_IDP_CLIENT_SECRET or PRIVACYFENCE_IDP_CLIENT_SECRET_FILE, not both.")`.
  - An unreadable secret file raises `ConfigurationError` naming the variable, never the content.
  - **A partial environment is an error, not single-user mode.** When any `PRIVACYFENCE_IDP_*`
    variable or `PRIVACYFENCE_ADMIN_EMAILS` is set but `PRIVACYFENCE_IDP_ISSUER` or
    `PRIVACYFENCE_IDP_CLIENT_ID` is missing, it raises `ConfigurationError("Set both
    PRIVACYFENCE_IDP_ISSUER and PRIVACYFENCE_IDP_CLIENT_ID, or neither.")`.
- Otherwise `None`.
- `org_identity.IdpConfig.from_org_config` (`org_identity.py:118`) reads `org_mode.idp_section(org_config)`
  instead of `org_config.get("idp")`.
- `org_mode.is_single_user(org_config)` (D9) becomes `resolve_mode(org_config) == "org" and not
  _idp_present(org_config)`.
  - `_idp_present` is a secret-free predicate: the bundle `idp` section is non-empty, or both
    `PRIVACYFENCE_IDP_ISSUER` and `PRIVACYFENCE_IDP_CLIENT_ID` are set.
  - It never reads the secret file, never warns, and never calls `from_org_config`, which does
    network discovery.
  - Only `IdpConfig.from_org_config` builds the full section through `idp_section`.
- `org_mode.py` gains a module logger (`logger = logging.getLogger(__name__)`). It must not
  import `org_identity`; the dependency already runs the other way (`org_identity.py:38`).
- `IdpConfig.client_secret` becomes `field(repr=False)` (`org_identity.py:85`), so a repr or a
  traceback of the config never shows it.
- The secret never appears in a log line, an audit entry, the Admin page or an error message. The
  docs recommend the `_FILE` form with a Docker secret, so the value does not show in `docker
  inspect`.

**Signing.**

- A bundle in org mode must still be signed (ADR 0016, `load_org_config`).
- An org server with no bundle at all, configured from the environment, is allowed. Its trust
  root is whoever controls the container's environment, the same party that chooses the image.
  ADR 0248 records this as an amendment to 0016.
- A signed bundle **without** an `idp` section refuses an IdP from the environment, unless the
  bundle carries `"idp": {"from_environment": true}` (D15 SEC-14).

**A server stays a server.** The first start for which `_idp_present` is true sets
`"org_server": true` in the admin config. It does this before IdP discovery, so a server whose IdP
is down still records it. `admin_config.apply_change` never changes this key, and the Admin page
does not show it as editable. D9's single-user refusal also fires when that flag is
set, so removing the IdP variables never turns a real server into single-user mode.

**Admins by email.** `IdpConfig` gains `admin_emails: tuple[str, ...]`, lower-cased, from the
section's `admin_emails` list.

- `org_identity.principal_from_claims` sets `is_admin` when the group rule matches, **or** when
  `isinstance(claims.get("email"), str)`, its `.strip().casefold()` is in `admin_emails`, **and**
  `claims.get("email_verified") is True`. The configured list is normalized the same way
  (`.strip().casefold()`).
- A multi-tenant issuer such as `accounts.google.com` accepts any Google account that has verified
  the address. The docs recommend pairing the email list with allowed domains (Admin → Sign-in), or
  an Internal consent screen.
- An IdP that does not send `email_verified` (Microsoft Entra ID, for example) cannot name admins
  by email. The docs tell those admins to use the group claim.
- `scripts/build_org_bundle.py` gains `--idp-admin-email` (repeatable), writing `idp.admin_emails`.
  This is IdP configuration, so it stays in the bundle.

**Docker.**

- `deploy/docker/privacyfence.env.example` is a new, commented example with the variables above
  plus `PRIVACYFENCE_ISSUER_URL` and `PRIVACYFENCE_TRUSTED_PROXIES=172.30.87.2`.
- The docs say one Google OAuth client can also serve Peek and Grist sign-in, by adding their
  redirect URIs to it. They recommend a separate client per service in the same Google project,
  so each secret can be rotated or revoked on its own.
- `compose.yaml` gains a commented `secrets:` example for `idp_client_secret`, mounted at
  `/run/secrets/idp_client_secret`.
- The entrypoint's "no bundle" message changes:
  - with both `PRIVACYFENCE_IDP_ISSUER` and `PRIVACYFENCE_IDP_CLIENT_ID` set: `No organization bundle: using the IdP and server from the environment.`
  - otherwise: the single-user message from D9.
- `docs/org-mode-docker.md` leads with the environment-only quick start, and keeps the bundle flow
  as the option for your own connector apps or a signed configuration.

### D15. Security hardening from the plan's security audit

The rules below come from a dedicated security audit of this plan. They override anything earlier
in the Design that contradicts them. Each names the phase that implements it; that phase's brief
points here.

**SEC-01: single-user mode does not protect against a local agent.** (p2, p16, p13b)

- An agent that can run `docker`, read `docker logs`, or open a page on another localhost port can
  take over a single-user install.
- `admin_config.import_from_bundle(..., single_user=True)` sets `step_up` to
  `{"enabled": true, "require_passkey": true, "scope": "writes_and_pii_reads"}`, the packaged-local
  defaults.
- p16 tests that a single-user approval, and every sensitive Admin action, return the step-up
  challenge when no assertion is sent, even with a valid session cookie.
- `LOCAL_MODE_DEPRECATION` and the docs (p13b) say plainly that anyone who can run `docker` on
  that computer controls the install. They recommend running it under a separate OS account
  (rootless Docker) or a VM, or using an organization server.
- `docs/security-and-compliance.md:19-21` ("no PrivacyFence-operated infrastructure") and
  `:66-67` are rewritten for the relay and for single-user mode.

**SEC-02: brand-trusted consent phishing.** (p11, p13b, p12)

- The provider docs never tell anyone to trust or pre-approve the built-in app. They say instead:
  `Do not mark the built-in PrivacyFence app as trusted or pre-approved: that trust would extend
  to every PrivacyFence server and to anyone using the public app. To trust an app, register your
  own.`
- The Worker refuses, with 400 `host_not_public`, any host whose registrable domain is in the
  private section of the Public Suffix List (`x.github.io`, `a.pages.dev`, `a.workers.dev`,
  `a.netlify.app`, ngrok domains and so on).
  - `cloudflare/oauth-relay/src/psl_private.ts` is generated by
    `cloudflare/oauth-relay/scripts/update-psl.mjs` from `public_suffix_list.dat` at a pinned
    commit, recorded in the file header.
  - Its test covers the hosts above.
- `docs/oauth-relay.md` gains a "Review new registrations" step: act on each SEC-23 notification
  email, plus a weekly look at `/registrations`. It also sets a revocation turnaround of one
  working day after a report.
- ADRs 0239 and 0243 record this threat precisely.

**SEC-23: every new registration emails the maintainers.** (p11, p13c, manual mb2, ma0) There
is no waiting period: a new host is served at once, and the maintainers hear about it right away.

- **Sending.** The Worker sends through Cloudflare Email Routing, with no third-party mail
  provider and no API key.
  - `wrangler.toml` has a `[[send_email]]` binding `name = "NOTIFY"` and
    `destination_address = "info@privacyfence.eu"`. The binding can only deliver to that address,
    which must be a verified destination in Email Routing.
  - The sender is `relay-notify@relay.privacyfence.eu`. Email Routing is enabled on the subdomain
    `relay.privacyfence.eu` only, so the MX records of `privacyfence.eu` and its existing mail are
    not touched.
- **When.**
  - An email is sent when a host is verified for the first time, which inserts its row.
  - Re-verification of a known host sends nothing.
  - A hard cap of 50 notification emails per UTC day. The first registration over the cap sends
    one email, `More new registrations today than the notification limit; see /registrations.`,
    and the rest of that day sends none.
- **Content.** Plain text, built by hand as an RFC 5322 message in `src/notify.ts`, with no
  dependency.
  - `Subject: New PrivacyFence relay registration: <host>`.
  - The body lists:
    - the host;
    - the UTC time;
    - the registering request's `CF-Connecting-IP` and `CF-IPCountry`, to judge abuse;
    - the total count of active registrations;
    - the revoke command, `curl -X POST -H "Authorization: Bearer $REVOKE_TOKEN" -H
      "Content-Type: application/json" -d '{"host":"<host>"}'
      https://oauth.privacyfence.eu/admin/revoke`.
  - Headers are `From`, `To`, `Subject`, `Date`, `Message-ID` (`<uuid@relay.privacyfence.eu>`),
    `MIME-Version`, and `Content-Type: text/plain; charset=utf-8`.
  - The host is already validated as ASCII by the host rules, so no header injection is possible.
    The code still refuses to send if any header value contains `\r` or `\n`.
- **Never blocks a registration.** Sending runs in `ctx.waitUntil`. A failure is caught and
  counted in the Durable Object (`notify_failures`, `last_notify_failure_at`). Both are shown in
  the maintainer-only `/registrations` response, so failures are visible although the Worker
  keeps no logs.
- **Tests** (stub `NOTIFY` by passing `{...env, NOTIFY: stub}` to the fetch handler):
  - a first registration sends exactly one message with the host, IP and revoke command;
  - a re-verification sends none;
  - the 51st first registration of a day sends the one over-the-limit message, and the 52nd
    sends nothing;
  - a throwing `NOTIFY` still returns 200 and increments `notify_failures`;
  - a header value with a newline is refused.
- **The weekly review stays** (SEC-02). The email is the prompt to act on a suspicious host the
  same day.
- **Privacy.** The privacy page (p13c) says the relay emails the maintainers the registering
  server's host name, the request's IP address and country when a new server registers.

**SEC-03: registration denial and storage flooding.** (p11, p5)

- **Rate limit.** The Worker rate-limits `/register` per `(host, CF-Connecting-IP)`, and adds a
  per-IP limit of 30 requests per 10 minutes, kept in the Durable Object.
- **No blocking the owner.** An attempt never blocks a later successful proof: `too_soon` applies
  only to repeated failures from the same IP for the same host.
- **No rows for unverified hosts.** The `registrations` table gets a row only on successful
  proof. Failed attempts are counted in a separate `attempts` table, pruned of rows older than one
  hour on each write.
- **The server keeps its status.** `relay_client` treats `too_soon` as "retry later" without
  clearing an earlier success. It persists `last_ok_at` to `org/relay_status.json`
  (`atomic_write_json`), so a restart within 35 days keeps relayed sign-in open.
- **Tests:** an attacker's register never makes the owner's next valid proof fail; unverified
  attempts create no `registrations` row.

**SEC-04: the Admin page always needs a passkey for sensitive actions.** (p7a, p7b, p6)

- Every Admin action marked sensitive in D6, and the room and bundle uploads, require a fresh
  WebAuthn assertion whatever the `step_up` configuration says.
- `routes_settings._apply_step_up_gate` gains `always: bool = False`. With `always=True`, a
  sensitive action requires an assertion even when `step_up.enabled` or `require_passkey` is
  false.
- A principal with no passkey gets 403 `{"error": "Add a passkey on /security first.",
  "enroll_url": "/security"}`.
- In org mode `set_server_address` refuses `http://` addresses and hosts that are IP literals or
  resolve to private, loopback or link-local addresses, with `Use an https:// address on a public
  host name.`
- **Tests:** on the default org step-up configuration, a sensitive Admin action without an
  assertion is refused; an org http or loopback server address is refused.

**SEC-05: what a compromised relay or deploy path can do.** (p11, p8a, p12, p13b, manual mb2)

- The `oauth-relay-worker` GitHub environment gets required reviewers, with "prevent
  self-review". The docs recommend the same for `downloads-worker`.
- The relay stays in the existing Cloudflare account, by maintainer decision. ADR 0243 and
  `docs/oauth-relay.md` record that a Workers deploy token covers every Worker in the account, so
  the relay token could replace the download Worker and the reverse. Required reviewers are the
  control.
- Slack and Miro send no PKCE. A compromised relay could exchange their codes first, or inject an
  attacker's own account. ADR 0243 and `security-and-compliance.md` state this per provider.
- The Admin page's Slack and Miro rows, when `relay` is true, add the line: `Sign-in codes for
  this service pass through oauth.privacyfence.eu without PKCE. Use your own app to avoid this.`

**SEC-06: audit forwarding cannot be redirected to an attacker with the token attached.** (p6)

- When `set_audit_forwarding` changes `kind`, or the scheme, host or port of `http_url`, it sets
  `http.bearer_token_env` to `""`.
- The summary is then `Changed audit forwarding; the bearer token was removed. Set it again in
  org/admin_config.json.`
- **Test:** a sentinel environment value is never sent to a changed URL.

**SEC-07: admins and allowed domains need verified, organization-bound email.** (p20b)

- **Google.** When the issuer is `https://accounts.google.com`, an email admin also needs
  `claims.get("hd")` to equal the email's domain. A personal Google account registered with a
  work address has no `hd`.
- **Allowed domains.** `org_identity.check_authz_policy` trusts the email domain only when
  `email_verified is True`, plus the same `hd` rule for Google. This changes existing behaviour,
  and the CHANGELOG says so.
- **Normalization.** Admin-email matching uses ASCII `.strip().lower()` and refuses addresses
  with non-ASCII characters. It does not use `casefold`, which maps `ſ`→`s`.
- **Tests:** a consumer account without `hd` is not admin; `email_verified: false` with an
  allowed domain is denied; a non-ASCII address never matches.

**SEC-08: reserved principal ids.** (p20b)

- `principal_from_claims` maps a `sub` equal to `local` or `single-user`, or starting with `os-`,
  to `"idp-" + sha256(sub).hexdigest()[:32]`.
- When an IdP is added to a data directory holding `users/single-user/`, startup logs that this
  data is not reachable by IdP users.
- **Tests:** each reserved form is hashed.

**SEC-09: no public directory of servers.** (p11, p5, p13c)

- **`GET /registrations`** requires `Authorization: Bearer <REVOKE_TOKEN>`, the maintainer token.
  Without it the answer is 401. The privacy page and the docs no longer mention a public list.
- **The well-known route** returns 404 unless `relay_client.status()["needed"]` is true, so a
  server whose admin never offered a built-in service cannot be registered by someone else.

**SEC-10: strict relay parsing.** (p11)

- **Loopback hosts** match exactly `^(localhost|127\.0\.0\.1)(:([1-9][0-9]{0,4}))?$`, with a port
  of at most 65535.
- **Repeated parameters.** A request with more than one `state`, `code` or `error` parameter gets
  400.
- **Paths.** `/callback/:grant` matches the exact path only; `/callback/slack/x` gets 404.
- **Location.** It is built with `new URL()`, and its `.host` must equal the validated host.
- **Negative tests:**
  - host forms: `localhost.evil.com`, `localhost:80@evil.com`, `127.0.0.1.nip.io`, `LOCALHOST`,
    `[::1]`;
  - ports: `localhost:0`, `localhost:099`, `localhost:65536`;
  - encoding and repeats: `localhost%2F@evil.com`, a duplicate `state`, a sub-path.

**SEC-11: a later IT bundle never overwrites Admin-page decisions.** (p2, p20a)

The `ensure` re-import is a merge, not a replacement:

- It copies a legacy section only when the current config lacks it or holds its default.
- It never lowers `step_up` (reuse the `tighten_step_up` comparison).
- It keeps `services[*].offered` true where it already is.
- It always keeps `org_server`.
- Each overwritten section name is logged and audited.

**SEC-12: key rotation keeps working after adoption.** (p0, p13a)

- Legacy pins are copied only while `org_bundle_adopted.json` lacks `"pins_adopted": true`.
- The first pin copy writes the record, with the bundle part empty if there was no bundle.
- After that, deleting the new-name pin (documented rotation) never brings the old key back.
- **Test:** rotation after adoption accepts a bundle signed with the new key and does not re-pin
  the old one.

**SEC-13: built-in secrets: fewer copies and a rotation plan.** (p1, p12, p11)

- **Desktop installers ship no `google_web`.** `scripts/builtin_apps.py write` gains
  `--exclude KEY` (repeatable). `build_dmg.sh`, `build_deb.sh` and `build_installer.ps1` call it
  with `--exclude google_web`. The org image's wheel and the PyPI build include it, because
  servers use them.
- **Rotation.** ADR 0239 and `docs/oauth-relay.md` (a "Rotating a built-in app secret" section)
  describe rotation: add the new secret alongside the old where the provider allows two (Google
  and Salesforce do), release, then retire the old one after the release is adopted.

**SEC-14: a signed bundle locks the IdP; no wildcard proxies.** (p20a, p15)

- **The IdP.** When a signed bundle is present without an `idp` section, `PRIVACYFENCE_IDP_*` and
  `PRIVACYFENCE_ADMIN_EMAILS` are refused with a `ConfigurationError`, unless the bundle carries
  `"idp": {"from_environment": true}`. `build_org_bundle.py` gains
  `--idp-from-environment` (p20b) to write it.
- **Trusted proxies.** `trusted_proxies` from any source refuses `*`, `0.0.0.0/0` and `::/0` with
  `trusted_proxies must name specific proxies, not a wildcard.`

**SEC-15: "no logs" means the Worker writes none.** (p11, p12, p13c)

- The privacy page and ADR 0243 say "the Worker writes no logs".
- The runbook checks, as setup and on each review:
  - that no Logpush job and no logging WAF rule cover `oauth.privacyfence.eu`;
  - that `wrangler tail` access is limited to the maintainer token.

**SEC-16: the setup code is single-use under concurrency.** (p16)

- The check and the consume happen under one `threading.Lock`.
- **Test:** two concurrent correct submissions, of which exactly one succeeds.
- The docs say the code stops working once a passkey is enrolled.

**SEC-17: no wildcard bind address in the Host allowlist.** (p15)

- `web/server.py` (`:1610`) never adds `0.0.0.0` or `::` to `allowed_hosts`.
- **Test:** `Host: 0.0.0.0` is refused when bound to `0.0.0.0`.

**SEC-18: server state never comes from the request.** (p6, p7a)

- The signature is `admin_config.apply_change(action, params, *, bundle, mode, single_user)`.
- The dispatcher passes only the action's declared parameters, and rejects unknown body keys
  (other than `csrf` and `webauthn_assertion`) with 400.
- **Test:** a body carrying `bundle` is refused.

**SEC-19: refreshing the shared config is atomic for readers.** (p3, p5, p7a)

- `oauth_apps.refresh_in_place` runs under a module lock.
- `oauth_apps.snapshot(org_config) -> dict` returns `dict(org_config)` under the same lock.
  Readers that iterate over the config (`org_stats.collect`, the connect page's section loop)
  use the snapshot.
- The pending connect entry also stores the section's `client_id`. The callback fails cleanly,
  with the existing `/connect?error=` path, when it changed.

**SEC-20: room text is untrusted.** (p6)

- `replace_rooms` refuses control characters, newlines included, in string values.
- ADR 0241 says room text reaches agents as untrusted data.

**SEC-21: the marker is advisory; backups are private.** (p19a, p12)

- `<file>.pre-canonical-verbs.bak` and `.pre-scope-rules.bak` are written with mode 0600 through
  `secure_files`.
- ADR 0246 requires 8.x to keep reading pre-7 shapes fail-closed rather than trusting the marker,
  and its installers not to follow symlinks when reading it.

**SEC-22: public secrets and refresh tokens.** (p12)

ADR 0239 notes that with built-in apps a stolen refresh token no longer also needs the org's own
client secret.

## ADRs

- **0239** — Built-in PrivacyFence OAuth apps for Google, Slack, Atlassian, Salesforce and Miro
  ship in every distribution, baked in from CI secrets by `scripts/builtin_apps.py`. Relates to
  0040: same mechanism, new apps; 0040 gets no status line.
- **0240** — `org_config.json` holds only the startup settings (`mode`, `server`, `idp`) and app
  credentials, stays signed, and a provider it does not carry uses the built-in app. Amends 0196
  (which services are offered is no longer read from the bundle) and 0016 (the signature no longer
  covers operational settings).
- **0241** — Operational settings live in a daemon-written `org/admin_config.json`, imported once
  from an existing bundle and then managed on the Admin page.
  - On a new org server the built-in services start off; offering the first one is the opt-in to
    the relay.
  - The audit-forwarding bearer-token variable name is not editable from the page.
  - It has settings.yaml-level trust.
  - Changes need the install owner (local) or an IdP admin (org), plus step-up for sensitive ones.
  - Step-up can only be made stricter from the page; relaxing it is still a file edit, so ADR 0068
    holds.
  - On a desktop install the owner, not the bundle's author, now decides which services are
    offered and whether unattended sessions are on.

  Amends 0196; relates to 0016, 0034, 0068, 0070, 0200.
- **0242** — Administration is a separate top-level Admin page; Settings shows every person the
  same personal sections. The org Privacy and Stats sections move to Admin. Amends 0217 (where
  Stats lives); relates to 0033.
- **0243** — Org servers use the built-in apps through the PrivacyFence-hosted redirect relay at
  `oauth.privacyfence.eu`. It is deployed from `main` on its own track, never by a release tag,
  and its contract only grows. It forwards only to hosts that proved control of their domain, keeps no
  logs, shows registrations only to maintainers, and maintainers can revoke a host. It runs in the
  existing Cloudflare account, with required reviewers on deploys. It forwards to a loopback
  host without registration. Relates to 0011, 0186.
- **0244** — `mode` and the server address may come from environment variables. Organization mode
  without an IdP is single-user mode, with one administrator,, allowed only on `http://localhost` or
  `127.0.0.1`, signed in with a one-time setup code and then a passkey. The Docker image starts in
  organization mode. Relates to 0011, 0177, 0213.
- **0245** — Local (desktop) mode is deprecated in 7.x, announced in the log, status, pages and
  companion, and is to be removed in 8.x. Relates to 0002, 0003.
- **0246** — 7.x is the migration checkpoint.
  - Every start migrates every person's data eagerly, then records `data_format.json`
    (`format: 7`).
  - 7.x refuses data whose format is newer than its own.
  - 8.x may delete the pre-7 converters and refuses data without the marker, telling people to
    install 7.x first.

  Relates to 0047, 0171, 0172, 0228, 0236, 0041.
- **0247** — The 6 → 7 upgrade is reversible. 7.x adopts `org_config.json` and its pin files by
  copying them to new names (`org_bundle.json`, `org_bundle_signing_pubkey.txt`,
  `org_bundle_serial.txt`) and never changes a 6.x file, so the latest 6.x keeps running after a
  downgrade. Amends 0016 and 0200 (the file names); relates to 0246.
- **0248** — An organization server can take its IdP and server address from environment
  variables, with no bundle. A bundle, when present, must still be signed and wins. A signed bundle
  without an `idp` section refuses an environment IdP unless it sets `idp.from_environment`. Amends 0016
  (org mode no longer always needs a signed bundle) and 0011.
- **0249** — Admins can be named by IdP-verified email (`PRIVACYFENCE_ADMIN_EMAILS` or
  `idp.admin_emails`), alongside the group claim. Relates to 0011.

## Manual steps

Step-by-step page: https://claude.ai/artifact/TumFGa3FRUqYkvuQyuTkGB

- **Before:**
  - (mb1) Register the seven PrivacyFence-owned apps and store their 14 values as repository
    secrets.
  - (mb2) Create the Cloudflare token and the relay Worker's GitHub environment.
  - (mb3) Optionally, start Google's verification.
- **After:**
  - (ma1) Sign in with the built-in apps on a desktop build.
  - (ma2) Deploy the relay from the branch and connect through it from an org server.
  - (ma3) Upgrade an org server with an old-style bundle, then use the Admin page and check that
    Settings looks the same for an admin and a normal user.
  - (ma4) Run the branch's Docker image in single-user mode: setup code, passkey, a connector
    through the relay, and an MCP client signing in. Check that a desktop install shows the
    deprecation notices.

## Risks and open questions

- **Security decisions the maintainer made from the audit (D15).**
  - Single-user mode is hardened (step-up on) and documented as no protection against an agent
    that can run docker.
  - Sensitive Admin actions always need a passkey.
  - There is no public registration list.
  - The relay stays in the existing Cloudflare account, behind required reviewers.
  - `google_web` ships only in server builds.
  - A signed bundle locks the IdP.
  - A delay before newly registered hosts can receive sign-ins was considered and not adopted.
    Instead, every new registration emails the maintainers (SEC-23), and review and revocation
    cover phishing hosts.
  - Slack's and Miro's lack of PKCE through the relay is an accepted, documented risk.

- **An environment-only server has no signature.** Its trust root is whoever controls the
  container's environment, which is the same party that picks the image. ADR 0248 states this. A
  bundle stays available for operators who want a signed configuration.
- **Admins by email depend on `email_verified`.** An IdP that does not send it cannot use the email
  list, and its admins use the group claim instead.

- **Downgrade target is the latest 6.x.** D12 rewrites rules into formats 6.9.x reads: version-3
  rows, canonical verbs, tokens with their own rules. An older 6.x release may not read them.
  The docs name 6.9.x as the release to go back to.
- **The 7.x docs use the new file name.** An operator who follows old instructions and copies a
  bundle to `org_config.json` after upgrading gets the "changed after the upgrade and is ignored"
  warning at every start. That is deliberate: it is better than 7.x silently reading a file that
  6.x also owns.

- **A principal whose data cannot be converted blocks the marker.** Examples are a v1 section
  without the conversion marker, or an unreadable rule. That install cannot move to 8.x until the
  data is fixed, by design. The log, the Admin page and the docs name the problem; nothing is
  silently skipped.

- **Single-user safety relies on the port publish.** Inside the container the daemon binds
  `0.0.0.0`. Only `-p 127.0.0.1:8765:8765` keeps it off the network, and the image cannot verify
  that. The issuer must be loopback and the account needs a passkey, so a reachable single-user install still
  needs the setup code from the container log. ADR 0244 and the Docker docs say this plainly.
- **Single-user sign-in is new authentication code.** p16 runs on Opus for that reason (see its
  `worker_model_reason`).

- **Fresh org servers start with built-in services off.** Relay registration starts only once an
  admin offers a built-in service. Single-user mode and a desktop install
  start with them on.
- **A bundle that adds a provider later** does not offer it by itself after the import. An admin
  switches it on, and the docs say so.
- **Desktop installs lose bundle enforcement of the offered services.** An IT-distributed bundle
  used to decide which services a desktop install offers. After the import, the install owner
  decides on the Admin page. This follows from moving the switches out of the signed bundle, and
  ADR 0241 records it.
- **Signing no longer covers the operational settings.** `authz`, `step_up`, `audit_forwarding`
  and `scoped_tokens` were covered by the bundle signature and now are not. They have the same
  protection as the install-wide settings.yaml. ADR 0240 and 0241 record this, and step-up can
  only be tightened from the UI.
- **Something may treat a non-empty `org_config` as "has a bundle".** The effective config is
  never empty-equivalent to a missing bundle. p4 greps truthiness checks; the planner and reviewer
  found only harmless ones. A hit that decides something else stops p4.
- **Org-mode objects may copy `org_config`** instead of sharing it, which `refresh_in_place` would
  not reach. The reviewer confirmed one shared dict; p5 greps again.
- **Restart-only settings.** Most org operational settings are read at startup, so an Admin change
  to them needs a restart. The page says so (D6). Making them live is out of scope.
- **Provider constraints the code cannot enforce.** The docs (p13b) cover them:
  - Google shows an unverified-app screen, and caps the app at 100 users, until it is verified.
    Gmail and Drive scopes are restricted, so verification includes an annual third-party
    security assessment (CASA).
  - Salesforce orgs block uninstalled connected apps by default.
  - Slack workspaces may need an admin's approval.
- **Switching apps** (a bundle adding or removing a provider's credentials) makes people reconnect
  Salesforce, Atlassian and Miro, whose refresh uses the current app's credentials.

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
  why: the relay's deploy workflow (p11) uses them once the PR reaches main, and ma2 deploys from the branch by hand with the same token
  done_when: the repository has an environment named oauth-relay-worker, restricted to main, holding secrets CF_OAUTH_RELAY_WORKER_API_TOKEN, CF_OAUTH_RELAY_WORKER_ACCOUNT_ID and CF_OAUTH_RELAY_REVOKE_TOKEN, and the API token verifies with the curl in the page
- id: mb3-google-verification
  title: (Optional, long-running) Submit the Google consent screen for verification
  why: until verified, Google shows an unverified-app screen and caps the app at 100 users; nothing in the plan waits for it
  done_when: the Google Cloud console's Verification Center shows the submission, or the user chose to defer it
manual_after:
- id: ma0-relay-first-deploy
  title: Deploy the relay Worker for the first time from the feature branch, set its revoke token, and run the runbook's smoke tests
  why: the deploy workflow deploys only main, and ma2 and ma4 need the real relay at oauth.privacyfence.eu before the PR merges
- id: ma1-desktop-builtin-signin
  title: Install a desktop build of the feature branch (from a dispatched build.yml run) with no organization bundle, and sign in to Gmail, Slack, Salesforce, Jira and Miro with the built-in apps
  why: real provider consent screens and loopback redirects cannot run in CI
- id: ma2-org-relay-signin
  title: On a test org server whose bundle has only mode/server/idp, offer the built-in services, register with the relay (deployed in ma0) and connect Gmail, Slack, Salesforce, Jira and Miro through it
  why: the relay round trip needs a public domain, the real Worker and real providers; the deploy workflow only deploys main
- id: ma3-upgrade-and-admin-page
  title: Upgrade an org server that has an old-style bundle, check the one-time import, use the Admin page, and compare Settings for an admin and a normal user
  why: checks the migration and the admin path end to end, with step-up, on a real deployment
- id: ma4-docker-single-user-and-deprecation
  title: Run the branch's Docker image in single-user mode (setup code, passkey, a connector through the relay, an MCP client), and check a desktop install's deprecation notices
  why: single-user mode's browser ceremonies, the real relay and a real MCP client cannot run in CI
verify_after_merge:
- python3 -m mypy src/privacyfence
- python3 scripts/mypy_strict_modules.py
- test ! -d cloudflare/oauth-relay || (cd cloudflare/oauth-relay && npm ci && npm run typecheck && npm test)
final_checks:
- docs/builtin-oauth-apps-plan.md and docs/builtin-oauth-apps-plan-manual-steps.html are deleted, and grep -rn "builtin-oauth-apps-plan" --exclude-dir=.git . finds nothing
- ADRs 0239-0249 (or their renumbered successors) exist, are Accepted, and are listed in docs/adr/README.md's index; ADRs 0011, 0016, 0196, 0200 and 0217 carry new Status lines; ADR 0040 is unchanged
- CHANGELOG.md has the new entries under "## [Unreleased]" and no new version heading
- cd cloudflare/oauth-relay && npm ci && npm test && npm run typecheck && npm run dry-run passes
phases:
- id: p0-bundle-file-rename
  title: 7.x reads org_bundle.json, adopted by copying org_config.json and its pin files, so a downgrade to 6.x keeps working
  depends_on: []
  complexity: M
  touches:
  - src/privacyfence/org_bundle_signing.py
  - tests/unit/test_org_bundle_signing.py
  - src/privacyfence/paths.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  - src/privacyfence/settings_controller.py
  - tests/unit/test_settings_controller.py
  - tests/unit/web/test_routes_settings.py
  - deploy/docker/healthcheck.py
  - deploy/docker/docker-entrypoint.sh
  - scripts/build_org_bundle.py
  - tests/unit/test_build_org_bundle.py
  - scripts/qa_authenticate_connectors.py
  - tests/unit/test_qa_authenticate_connectors.py
  - src/privacyfence/org_mode.py
  - tests/unit/test_org_mode.py
  - src/privacyfence/step_up_config.py
  - tests/unit/test_step_up_config.py
  - tests/conftest.py
  brief: |-
    Read "Design → D13" in full. Every name, message and rule is there.
    1. org_bundle_signing.py: switch PINNED_PUBKEY_FILENAME and PINNED_SERIAL_FILENAME to the 7.x names, add the two LEGACY_ constants, and add adopt_legacy_files(org_dir) per D13. The pins are copied when their new name is missing, independently of the bundle, but only until org_bundle_adopted.json records "pins_adopted": true (D15 SEC-12). Use secure_files.atomic_write_bytes, and write org_bundle_adopted.json with atomic_write_json.
    2. paths.py: add ORG_BUNDLE_FILENAME, LEGACY_ORG_CONFIG_FILENAME and org_bundle_path().
    3. daemon_main.py:
       - load_org_config calls adopt_legacy_files(org_dir()) first, sets _bundle_adopted_this_start with |= (only ever to True), and reads paths.org_bundle_path().
       - Add the "changed after the upgrade and is ignored" warning per D13.
       - log_org_config_bundle_hash reads the new path.
       - run_app records the org_bundle_adopted audit entry right after log_org_config_bundle_hash.
       - Update the error texts that name org_config.json in load_org_config to name org/org_bundle.json.
       - Do the same for every user-facing message D13 lists: org_mode.py (:47-512) and step_up_config.py (:293, :299) say org_bundle.json. Update the tests that pin those texts. Leave web/mcp_dispatch.py and the sync runner to p18.
    4. settings_controller.py: install_org_config_bytes writes paths.org_bundle_path() (:1145). The snapshot's installed date reads it (:2108), and uses the legacy file's mtime while the bundle still equals the adopted copy (D13).
    5. deploy/docker/healthcheck.py and docker-entrypoint.sh per D13: the healthcheck reads the new name and falls back to the old one, and the entrypoint counts either file as installed. Keep the entrypoint message's prefix "No organization bundle at" (tests/integration/test_org_docker_image.py:188 asserts it), followed by the new path.
    6. scripts/build_org_bundle.py: the --output default becomes org_bundle.json. scripts/qa_authenticate_connectors.py: DEFAULT_ORG_CONFIG_PATH becomes the new name; the backup at :119 uses DEFAULT_ORG_CONFIG_PATH.name; the docstring (:24, :41, :107) names the new file.
    7. tests/conftest.py: reset daemon_main._bundle_adopted_this_start in _reset().
    8. Tests:
       - test_org_bundle_signing.py:
         - adoption copies the bundle bytes and both pins, writes the record, and leaves the legacy files byte-identical with unchanged mtimes;
         - no adoption when org_bundle.json exists;
         - a failed copy raises ConfigurationError;
         - pins are read and written only under the new names after adoption.
       - test_daemon_main.py:
         - a data dir with only org_config.json loads through adoption and audits org_bundle_adopted once;
         - a changed org_config.json after adoption logs the "ignored" warning and is not read;
         - a downgrade simulation: after a 7.x start that installs a newer-serial bundle, the legacy serial pin and org_config.json are untouched.
       - Existing tests:
         - tests/unit/test_daemon_main.py: switch the _write_signed_bundle helper (~:370) and every org_config.json write to the new name. Tests that change the bundle after a first load, patch open() for the bundle path, or call log_org_config_bundle_hash directly would otherwise pass for the wrong reason or fail: :342, :415-437, :479-512. Keep the legacy name only in the new adoption tests.
         - tests/unit/test_settings_controller.py and tests/unit/web/test_routes_settings.py: retarget every org_dir()/"org_config.json" path to the new name, including the `assert not (...).exists()` checks (test_settings_controller.py:667, 673, 758, 849; test_routes_settings.py:1029, 1039, 1099, 1128, 1206, 1220, 1268, 1298), which would otherwise always pass. An upload's own filename field may stay "org_config.json".
         - test_build_org_bundle.py and test_qa_authenticate_connectors.py: the new default name.
         - Do not weaken any assertion.
    9. Run ruff check . and python3 -m pytest tests/unit -q.
    Stop condition: if more than 40 existing tests need edits beyond a file name, stop with status=blocked and list them.
    Also apply D15 SEC-12, with its test.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/test_org_bundle_signing.py -q -k adopt passes and selects at least 4 tests
  - grep -rn '"org_config.json"' src/privacyfence | grep -v LEGACY_ORG_CONFIG_FILENAME finds nothing
  - git grep -n 'org_dir() / "org_config.json"' tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py tests/unit/test_daemon_main.py finds nothing outside the adoption tests
  - ruff check . passes
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
  brief: |-
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

    Also apply D15 SEC-13 (desktop installers): add `--exclude KEY` to scripts/builtin_apps.py write, and pass `--exclude google_web` in build_dmg.sh, build_deb.sh and build_installer.ps1, with a test that the excluded key is absent from the module and that those three scripts pass it.
  acceptance:
  - python3 -m pytest tests/unit/test_builtin_apps_script.py tests/unit/test_app_credentials.py tests/unit/test_telegram_credentials.py tests/unit/test_org_image_workflows.py -q passes
  - grep -c "scripts/builtin_apps.py write" .github/workflows/build.yml .github/workflows/publish-pypi.yml scripts/build_dmg.sh scripts/build_deb.sh scripts/build_installer.ps1 reports at least 1 for each file
  - grep -n "src/privacyfence/_builtin_apps.py" .gitignore MANIFEST.in matches in both files
  - 'the dispatched build.yml run''s "Build .deb" log contains "built-in app google_desktop: included"'
  - ruff check . passes
- id: p2-admin-config-store
  title: 'The admin config file: schema, load/save and the one-time import from a bundle'
  depends_on:
  - p1-builtin-apps-generator
  - p0-bundle-file-rename
  complexity: M
  touches:
  - src/privacyfence/admin_config.py
  - tests/unit/test_admin_config.py
  - tests/conftest.py
  - scripts/check_coverage_floor.py
  brief: |-
    Read "Design → D2", "D3" and "D8". Names, schema, import rules, the ensure exception, config_for, load normalization and messages are all there. This phase adds the module only; service_rows and apply_change come in p6, and no caller changes here.
    1. Create src/privacyfence/admin_config.py with AdminConfigError, SERVICES, SERVICE_LABELS, OPERATIONAL_SECTIONS, LEGACY_BUNDLE_KEYS, admin_config_path, load (with normalization), save, import_from_bundle(bundle, *, mode, single_user), ensure(bundle, *, mode, single_user), config_for(bundle, *, mode, single_user) and legacy_keys_in, exactly as D2 and D3 define them.
       - Use secure_files.atomic_write_json, and a module-level threading.Lock _write_lock around save.
       - Do not import oauth_apps at module level (it will import this module).
    2. tests/conftest.py: add the D8 autouse fixture _isolate_admin_config_and_builtin_apps, modelled on _isolate_whats_new_state, with D8's exempt-module rule. It patches admin_config.admin_config_path and app_credentials.builtin_app.
    3. scripts/check_coverage_floor.py: add "src/privacyfence/admin_config.py" to MODULE_FLOORS at 100, in the entry format the file already uses.
    4. tests/unit/test_admin_config.py. It is exempt from the fixture, so patch paths.data_dir to tmp_path in a module fixture.
       - A missing file loads as None.
       - A malformed file, a non-object, or a wrong version raises AdminConfigError without the content in the message.
       - Normalization fills a missing service and a missing key.
       - The saved file has mode 0o600 on POSIX; skip on Windows the way the existing secure_files tests do.
       - import_from_bundle:
         - an empty bundle in local mode, and in org single-user mode, offers google, slack, salesforce, atlassian, miro and telegram;
         - an empty bundle on a multi-user org server offers none;
         - grist and peek are never offered from an empty bundle;
         - a bundle with only slack and peek sections offers exactly those two and copies peek.base_url;
         - atlassian assets and assets_workspace_id are copied;
         - grist.server_url is copied only when grist has no client_id;
         - every OPERATIONAL_SECTIONS key is copied verbatim;
         - the hash is the documented SHA-256, and "" for {}.
       - ensure:
         - it imports and saves when the file is missing;
         - it returns (loaded, False) without writing when the file exists;
         - it re-imports exactly once when the stored hash is "" and the bundle has legacy keys.
       - config_for never writes.
       - legacy_keys_in returns the dotted names, including atlassian.assets and a grist.server_url without client_id, and [] for a credentials-only bundle.
    5. Run ruff check ., python3 -m pytest tests/unit -q (the new autouse fixture must break nothing), and python3 -m mypy src/privacyfence/admin_config.py.
    Also apply D15 SEC-01 (single-user step_up defaults in import_from_bundle) and SEC-11 (the ensure re-import is a merge with D15's four rules, logging each overwritten section), each with tests.
  acceptance:
  - python3 -m pytest tests/unit/test_admin_config.py -q --cov=privacyfence.admin_config --cov-branch --cov-report=term-missing passes and reports 100%
  - python3 -m pytest tests/unit -q passes
  - python3 -m mypy src/privacyfence/admin_config.py passes
  - ruff check . passes
- id: p3-effective-config
  title: Compose the effective org config from the bundle, the admin config and the built-in apps
  depends_on:
  - p2-admin-config-store
  complexity: M
  touches:
  - src/privacyfence/oauth_apps.py
  - tests/unit/test_oauth_apps.py
  - scripts/check_coverage_floor.py
  brief: |-
    Read "Design → D4" (the oauth_apps module part). The steps of effective_org_config are numbered there; implement them in that order.
    1. Create src/privacyfence/oauth_apps.py with RELAY_BASE_URL, RELAY_MARKER, GOOGLE_AUTH_URI, GOOGLE_TOKEN_URI, BUILTIN_KEY, AppSource, credentials_source, effective_org_config, refresh_in_place, relay_redirect_uri and uses_relay.
       - Call the built-ins as `app_credentials.builtin_app(...)` and `app_credentials.telegram_app_credentials()`, after `from . import app_credentials`, so the conftest patch covers them.
       - Read offered as `admin["services"].get(s, {}).get("offered", False)`.
       - Import admin_config at module level.
    2. scripts/check_coverage_floor.py: add "src/privacyfence/oauth_apps.py" to MODULE_FLOORS at 100.
    3. tests/unit/test_oauth_apps.py:
       - Legacy keys are dropped: every LEGACY_BUNDLE_KEYS key, atlassian.assets/assets_workspace_id and the API-key grist.server_url are absent from the result even when present in the bundle.
       - A withdrawn service's section is removed even when the bundle carries its credentials, and a service missing from admin["services"] counts as not offered.
       - Parametrized over google, slack, salesforce and miro × local and org × (bundle credentials, built-in present, built-in absent): the exact section, with relay True only for built-in in org mode; google carries auth_uri/token_uri; salesforce built-in uses consumer_key/consumer_secret.
       - atlassian combines credentials with the admin assets values, and has no section without credentials.
       - telegram gets {"enabled": True} only when telegram_app_credentials() is not None.
       - grist: a bundle OAuth app beats the admin server_url; the admin server_url alone; local with neither gives {"enabled": True}; org with neither gives no section.
       - peek: the same three cases with base_url.
       - Every OPERATIONAL_SECTIONS key is copied from admin.
       - A bundle "relay" key is stripped.
       - Neither input is mutated.
       - refresh_in_place keeps the same object.
       - credentials_source for each case.
    4. Run ruff check ., python3 -m pytest tests/unit/test_oauth_apps.py -q and python3 -m mypy src/privacyfence/oauth_apps.py.
    Also apply D15 SEC-19 (refresh_in_place under a module lock, and snapshot()), with a test that snapshot returns an independent copy.
  acceptance:
  - python3 -m pytest tests/unit/test_oauth_apps.py -q --cov=privacyfence.oauth_apps --cov-branch --cov-report=term-missing passes and reports 100%
  - python3 -m mypy src/privacyfence/oauth_apps.py passes
  - ruff check . passes
- id: p4-wire-startup
  title: Startup imports the admin config once and every reader sees the effective org config
  depends_on:
  - p3-effective-config
  complexity: M
  touches:
  - src/privacyfence/daemon_main.py
  - src/privacyfence/settings_controller.py
  - src/privacyfence/web/server.py
  - tests/unit/test_daemon_main.py
  - tests/unit/test_settings_controller.py
  - tests/unit/web/test_server.py
  - tests/unit/web/test_server_org_mode.py
  brief: |-
    Read "Design → D3" ("Where the import runs") and "D4" ("Where it is applied").
    1. Guard grep: `grep -rnE "(not|if|bool\()\s*org_config\b|len\(org_config\)" src/privacyfence`. The reviewer found these hits, all harmless:
       - daemon_main.py:558, which checks the signature field on the raw bundle;
       - daemon_main.py:1120, a docstring;
       - settings_controller.py:1295, 1385 and 1745;
       - routes_connect.py:141;
       - org_mode.py:535.
       If any other hit decides something from org_config being empty or non-empty, stop with status=blocked and name it.
    2. daemon_main.py:
       - Add load_admin_config(bundle) -> tuple[dict, bool] and record_admin_config_import(bundle, imported) per D3, passing single_user=False (p15 changes it).
       - Add effective_org_config(bundle, admin) per D4.
       - In run_app:
         - `bundle = load_org_config()`, then reject_removed_tool_annotations(bundle), then `admin, imported = load_admin_config(bundle)`.
         - resolve_mode, check_storage_permissions and init_privacy_filter use `bundle`.
         - Then `org_config = effective_org_config(bundle, admin)`; every later use takes it.
         - log_org_config_bundle_hash(bundle), then record_admin_config_import(bundle, imported).
       - The CLI dispatch near line 2518 uses the config_for form from D4, and exits on AdminConfigError the way it does on ConfigurationError.
       - Thread `bundle` and `startup_admin` down as new keywords (default None) on _maybe_start_web_server and _start_org_web_server, passed from run_app. Add two OrgAuth fields in web/server.py, `bundle: dict[str, Any]` and `startup_admin: dict[str, Any]`, both `field(default_factory=dict)`, set where OrgAuth is built. Nothing reads them yet.
    3. settings_controller.py:
       - _org_config_or_empty (~1203) and refresh_connectors' work (~1240) use the config_for form from D4. Import from .daemon_main and .admin_config inside the function, as the existing code does, and handle AdminConfigError like ConfigurationError.
       - install_org_config_bytes: after a successful install, if admin_config.legacy_keys_in(data) is non-empty and the stored admin config's imported_from_bundle_sha256 is not "", set self.error to D3's "are ignored" sentence. With a "" hash the next start re-imports, so show nothing.
    4. Tests.
       - test_daemon_main.py, a class TestAdminConfigStartup:
         - The first start with a bundle carrying authz and a slack section writes admin_config.json with only slack offered and authz copied, logs the import warning with n=1, and records an admin_config_imported audit entry after the bundle-hash entry.
         - A second start does not re-import, and logs the "are ignored" warning.
         - A malformed admin_config.json raises ConfigurationError.
       - test_daemon_main.py, a class TestEffectiveOrgConfigWiring:
         - log_org_config_bundle_hash receives the raw bundle (monkeypatch it).
         - With no bundle and a built-in slack app, build_connectors(config, org_config) reaches the Slack branch.
         - The CLI form uses google_desktop even when the bundle says mode org.
         - _start_org_web_server stores bundle and startup_admin on OrgAuth.
       - test_settings_controller.py:
         - The existing ORG_CONFIG_ENABLING_EVERY_CONNECTOR tests keep passing unchanged, because config_for imports from that bundle.
         - With no bundle and a built-in miro app, the snapshot lists Miro and authenticate_connector("miro") succeeds past the org check.
         - With miro withdrawn in admin_config.json, it is refused.
         - A bundle upload carrying "authz" over an imported config installs and sets the "are ignored" message.
       - Do not weaken any assertion.
    5. Run ruff check . and python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/test_daemon_main.py -q -k "TestAdminConfigStartup or TestEffectiveOrgConfigWiring" passes and selects at least 7 tests
  - ruff check . passes
- id: p5-org-relay-client
  title: Org servers sign in through the relay when a service uses a built-in app
  depends_on:
  - p4-wire-startup
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
  brief: |-
    Read "Design → D5": the contract, the host rules and "Org server side". Also read D8. The Worker is p11; here everything that talks to it is mocked.
    1. Guard grep: `grep -rnE "dict\(org_config\)|\{\*\*org_config|deepcopy\(org_config" src/privacyfence/daemon_main.py src/privacyfence/web`. The reviewer found none. If a copy of the org config is handed to routes_connect, build_org_routes, build_connectors or another long-lived object, stop with status=blocked and name it: refresh_in_place relies on one shared dict.
    2. Create src/privacyfence/relay_client.py per D5. It contains relay_token_path, well_known_token, valid_relay_host, public_host, register, status, request_check, _tick, start_registration_loop and reset_for_tests. Keep the module state in a small dataclass guarded by a threading.Lock, and never log the token.
    3. tests/conftest.py: call relay_client.reset_for_tests() inside _reset(), and extend the D8 fixture to patch relay_client.relay_token_path.
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

    10. Also implement D9's "The relay on localhost" rules on this side: register refuses a loopback issuer without calling the Worker and accepts http only on a loopback host; _tick treats a loopback issuer as not needing registration; start() skips the registration refusal for a loopback issuer. Test each in the files above.
    Also apply D15 SEC-03's relay_client part (too_soon keeps last_ok_at; persist it in org/relay_status.json and load it at startup), SEC-09's well-known rule (404 unless needed), and SEC-19's pending client_id check, each with tests.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/web/test_routes_connect.py -q -k TestRelay passes and selects at least 6 tests
  - python3 -m pytest tests/unit/test_relay_client.py -q --cov=privacyfence.relay_client --cov-branch --cov-report=term-missing passes and reports 100%
  - ruff check . passes
- id: p6-admin-config-changes
  title: Validated admin config changes and the Admin page's service rows
  depends_on:
  - p3-effective-config
  complexity: M
  touches:
  - src/privacyfence/admin_config.py
  - tests/unit/test_admin_config.py
  brief: |-
    Read "Design → D6": the action table, the validation rules, the bearer-token rule, the tighten_step_up rule, the summaries, the room upload rules, and the page state's services row fields. This phase is pure module code; the routes come in p7a and p7b.
    1. In src/privacyfence/admin_config.py add:
       - apply_change(action, payload) -> tuple[str, list[str]] for every action in D6's table except the four privacy actions, which stay with org_install_policy.
         - It loads the config (config_for with the payload's "bundle", "mode" and "single_user" keys, which the routes pass), validates, mutates, saves under _write_lock, and returns (summary, changed_sections).
         - changed_sections uses the D6 section names.
         - A no-op returns ("", []) and saves nothing.
       - The validation per D6:
         - each parser, and the ConfigurationError, GristClientError and PeekClientError mappings;
         - the scoped-tokens 1..365 check;
         - the comma- or newline-separated authz lists split, stripped, with empty entries dropped;
         - set_audit_forwarding keeps http.bearer_token_env unchanged;
         - the tighten_step_up rule with its exact message.
       - replace_rooms(data, *, bundle, mode, single_user) -> str per D6's room rules. It raises AdminConfigError("The file is not a room directory written by sync_room_directory.py.") for any rule violation, and AdminConfigError("A room directory can hold at most 5000 rooms.") for too many.
       - service_rows(bundle, admin, *, mode) with exactly D6's row fields, importing oauth_apps inside the function. Never a secret.
    2. tests/unit/test_admin_config.py:
       - Each action's stored result, summary and changed_sections.
       - Each parser's error surfaces without the "org_bundle.json's " prefix: a negative inline_max_bytes, an http audit URL, required_groups without groups_claim.
       - Scoped tokens 0 and 366 are refused.
       - set_audit_forwarding cannot change bearer_token_env, even if the payload carries one.
       - tighten_step_up accepts false→true and a wider scope; refuses true→false, a narrower scope and require_passkey true→false, with the exact message; an unchanged submission is a no-op.
       - set_server_address normalizes, clears, and maps a client error.
       - "Unknown service.".
       - replace_rooms: the happy path, a room without resource_email, a 501-character name, a nested object value, and 5001 rooms.
       - service_rows for each app_source, with the relay flag only in org mode for built-ins, grist locked when the bundle has an OAuth app, and grist's client_id rule.
       - No row, summary or log record (caplog) contains a client_secret or a bearer-token env var's value. Set a fake env var to a sentinel and assert it never appears.
    3. Run ruff check ., python3 -m pytest tests/unit/test_admin_config.py -q and python3 -m mypy src/privacyfence/admin_config.py.
    Also apply D15: SEC-18 (the apply_change(action, params, *, bundle, mode, single_user) signature, replacing the payload keys), SEC-06 (bearer env dropped on a URL or kind change), SEC-04's org server-address rule, and SEC-20 (no control characters in rooms), each with tests.
  acceptance:
  - python3 -m pytest tests/unit/test_admin_config.py -q --cov=privacyfence.admin_config --cov-branch --cov-report=term-missing passes and reports 100%
  - python3 -m mypy src/privacyfence/admin_config.py passes
  - ruff check . passes
- id: p7a-admin-dispatcher-org
  title: 'The Admin JSON API in org mode: action table, auth, step-up, audit, live refresh and the state endpoint'
  depends_on:
  - p5-org-relay-client
  - p6-admin-config-changes
  complexity: M
  touches:
  - src/privacyfence/web/routes_admin.py
  - tests/unit/web/test_routes_admin.py
  - src/privacyfence/web/routes_settings.py
  - tests/unit/web/test_routes_settings.py
  - src/privacyfence/connector_registry.py
  - tests/unit/test_connector_registry.py
  - src/privacyfence/web/server.py
  - scripts/check_coverage_floor.py
  brief: |-
    Read "Design → D6" except "Rendering", the room upload and the bundle upload. This phase builds the org-mode JSON API; local mode and the uploads are p7b, and the HTML page is p8a.
    1. connector_registry.py: add evict_all() (clears _hosts and _last_used under the lock), with a test in tests/unit/test_connector_registry.py.
    2. routes_settings.py: add the keyword `sensitive: bool | None = None` to _needs_step_up and _apply_step_up_gate per D6; existing callers are unchanged. Test in test_routes_settings.py that sensitive=True gates an action not in _SENSITIVE_ACTIONS, and sensitive=False skips one that is.
    3. Create src/privacyfence/web/routes_admin.py:
       - AdminAction and ADMIN_ACTIONS per D6's table, excluding the four privacy actions (p9a), and RESTART_LABELS per D6.
       - A mode-independent core: `_dispatch(principal, action, body, *, mode, bundle, org_config, refresh_live, ...) -> Response`. It covers coercion (reuse routes_settings._coerce), the step-up gate with sensitive=, apply_change (passing bundle, mode and single_user=False; p15 changes it), the audit under principal_scope, the `applies` handling, and the page-state response.
       - `_admin_state(*, mode, bundle, org_config, startup_admin, principal) -> dict` per D6's "Page state". Leave out privacy and stats; p9a adds them.
       - build_org_routes(*, sessions, step_up, step_up_origin, connector_registry, org_config, bundle, startup_admin): `POST /api/admin/{action}` and `GET /api/admin/state`, with D6's 401/403/404 rules and CSRF and origin checks copied from routes_settings.build_org_routes' settings_action.
       - ROUTE_CLASSIFICATION mapping each POST path to "action-dispatcher" or "bespoke-sensitive", and a RuntimeError at build time for an unclassified POST route, as routes_settings does (ADR 0014).
    4. web/server.py: mount routes_admin.build_org_routes in org mode next to the settings build_org_routes call, passing org.org_config (the shared dict), org.bundle, org.startup_admin and org.connector_registry.
    5. scripts/check_coverage_floor.py: add "src/privacyfence/web/routes_admin.py" to MODULE_FLOORS at 95.
    6. tests/unit/web/test_routes_admin.py (org):
       - No session: 401. A non-admin: 403. An unknown or local-only action: 404.
       - Each sensitive action needs step-up when step-up is required (copy the step-up test setup from test_routes_org_settings.py); withdraw_service and clear_rooms do not.
       - An admin's withdraw_service removes the section from the shared org_config dict (`is` the same object) and calls evict_all and request_check.
       - set_web_push saves, leaves org_config alone, and GET /api/admin/state then lists "Web push" in restart_needed.
       - An AdminConfigError gives 400 with its message.
       - A successful change writes one settings_change audit entry ending "(admin=<id>)"; a no-op writes none.
       - GET /api/admin/state never contains a sentinel secret placed in the bundle or a sentinel env var's value.
    7. Run ruff check . and python3 -m pytest tests/unit -q.
    Also apply D15: SEC-04 (the `always` keyword on _apply_step_up_gate, used by every sensitive Admin action; the enroll message), SEC-18 (only declared parameters; unknown body keys give 400) and SEC-19 (org_stats.collect gets oauth_apps.snapshot(org_config)), each with tests, including: on the default org step-up configuration, a sensitive Admin action without an assertion is refused.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/web/test_routes_admin.py -q passes and selects at least 12 tests
  - grep -n "ROUTE_CLASSIFICATION" src/privacyfence/web/routes_admin.py matches
  - ruff check . passes
- id: p7b-admin-dispatcher-local-and-uploads
  title: The Admin JSON API in local mode, and the room-directory and bundle uploads
  depends_on:
  - p7a-admin-dispatcher-org
  complexity: M
  touches:
  - src/privacyfence/web/routes_admin.py
  - tests/unit/web/test_routes_admin.py
  - src/privacyfence/web/routes_settings.py
  - tests/unit/web/test_routes_settings.py
  - src/privacyfence/web/server.py
  - tests/unit/web/test_server.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  brief: |-
    Read "Design → D6": "Getting the bundle and the startup state", "Live actions" (local), "Room directory upload" and "Bundle upload (local mode)".
    1. routes_admin.py:
       - build_routes(*, controller, sessions, step_up, step_up_origin, require_human_session, startup_admin, org_config) for local mode. It reuses p7a's core with D6's local rules: the bundle per request, require_human_session for sensitive actions, and the local live refresh (refresh_in_place on the startup org_config plus controller.refresh_connectors()). The state carries "error": controller.error.
       - POST /api/admin/rooms/upload in both builders, per D6, classified "bespoke-sensitive".
    2. routes_settings.py:
       - Refactor org_config_upload's body into handle_org_config_upload(request, *, controller, ..., state_builder) with unchanged checks. /api/settings/org_config/upload stays, with state_builder returning the settings snapshot as today; p8b removes it.
       - routes_admin's local builder mounts POST /api/admin/org_config/upload with state_builder returning the admin state.
       - In _settings_bridge_shim, add an intercepted upload_rooms action posting to /api/admin/rooms/upload, the same way install_org_config is intercepted. Leave install_org_config's path alone.
       - Add tests for the new admin path; the existing settings-path tests stay unchanged.
    3. web/server.py: add `startup_admin` and `org_config` keywords to WebServer.__init__ and build_app only. daemon_main.py passes them from _maybe_start_web_server's existing `org_config` keyword and the `startup_admin` keyword p4 added; org_config is the same dict object the new-principal factory closes over. Mount build_routes in local mode wrapped in _owner_only_routes, next to build_settings_routes, only when a controller exists.
    4. Tests:
       - test_routes_admin.py, local:
         - owner-only: a non-owner principal gets 404, as Settings does;
         - require_human_session applies to sensitive actions;
         - offer_service saves, refreshes the startup dict in place and calls controller.refresh_connectors;
         - org-only actions give 404.
       - Rooms upload in both modes: replaces rooms with step-up; refuses a non-room file and a file over 2 MB; in local mode refuses without a human session.
       - Bundle upload at the new path: the TOFU 409 and step-up still apply, and the response is the admin state including the "are ignored" error.
       - test_server.py: the local app mounts /api/admin/state for the owner.
    5. Run ruff check . and python3 -m pytest tests/unit -q.
    Also apply D15 SEC-04 to the room and bundle uploads (always=True), with tests.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -n "/api/admin/org_config/upload" src/privacyfence/web/routes_admin.py matches
  - grep -n "upload_rooms" src/privacyfence/web/routes_settings.py matches
  - ruff check . passes
- id: p8a-admin-page-and-services
  title: The Admin page, its nav item, and the Services section
  depends_on:
  - p7b-admin-dispatcher-local-and-uploads
  complexity: M
  touches:
  - src/privacyfence/web_shell.py
  - tests/unit/test_web_shell.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  - src/privacyfence/web/routes_admin.py
  - tests/unit/web/test_routes_admin.py
  - src/privacyfence/web/routes_settings.py
  - src/privacyfence/web/routes_connect.py
  - src/privacyfence/web/routes_approvals.py
  - src/privacyfence/web/routes_whats_new.py
  brief: |-
    Read "Design → D6": "The nav" and "Rendering". Every string is in D6.
    1. web_shell.py: add ORG_ADMIN_NAV_ITEMS and org_nav_items(is_admin), change wrap's is_org test, and add the Admin item to _NAV_ITEMS, per D6. Change the org nav call sites D6 lists to org_nav_items(principal.is_admin).
       - Tests in test_web_shell.py: org_nav_items(True) ends with Admin and org_nav_items(False) has none; wrap treats both org tuples as org; the local nav has Admin.
       - Fix any existing test_web_shell.py assertion that pins the exact local nav tuple, updating only that expected tuple.
    2. settings_window_html.py:
       - build_html gains `surface` and `_capabilities_for` gains the surface argument, per D6, with "surface" in the capabilities.
       - The admin surface's sections are services and, in local mode, org_config. Features is p8b; the org-only sections are p9b.
       - render() branches to renderAdmin(state) per D6. renderAdmin draws its own nav and section switch, with D6's keys and labels, and never calls renderNav or renderSection.
       - Add renderServices(state): toggleHtml for offer/withdraw, the D6 status lines and doc links, and the relay status line in org mode.
       - The admin surface gets its own not_applicable_actions per D6.
       - The settings surface is unchanged in this phase.
    3. routes_settings.py: give _settings_bridge_shim an `endpoint` parameter (default "/api/settings"); the admin surface passes "/api/admin". The intercepted uploads post to `<endpoint>/org_config/upload` and `<endpoint>/rooms/upload`.
    4. routes_admin.py: add GET /admin in both builders. It renders web_shell.wrap(build_html(state, surface="admin", initial_section="services", mode=..., is_admin=...)) with active="admin", copying how routes_settings renders GET /settings in each mode. In org mode, no session redirects to /login?next=/admin and a non-admin gets a 403 page saying "Only administrators can open this page.".
    5. Tests:
       - test_settings_window_html.py: the admin surface's section sets for local, org admin and org non-admin; every D6 services status line renders for its row; the relay line appears only in org mode; initial_section "services" is used.
       - test_routes_admin.py: GET /admin renders for the owner or admin with the Admin nav item active; an org non-admin gets 403; no session redirects to /login?next=/admin.
    6. Run ruff check . and python3 -m pytest tests/unit -q. If the session-start hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_browser_smoke.py -q.
    Also apply D15 SEC-05's Admin-page line for relayed Slack and Miro rows, with a test.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -n "def org_nav_items" src/privacyfence/web_shell.py matches
  - grep -n "function renderServices" src/privacyfence/settings_window_html.py matches
  - ruff check . passes
- id: p8b-admin-features-and-config
  title: The Admin page's Features and Organization configuration sections
  depends_on:
  - p8a-admin-page-and-services
  complexity: M
  touches:
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  - src/privacyfence/web/routes_settings.py
  - tests/unit/web/test_routes_settings.py
  - tests/unit/web/test_routes_admin.py
  brief: |-
    Read "Design → D6": the Features actions, "Room directory upload", "Bundle upload (local mode)" and "Rendering".
    1. settings_window_html.py:
       - Add the features section to the admin surface in both modes, with renderFeatures(state):
         - the Assets form (set_atlassian_assets);
         - the Grist and Peek address forms (set_server_address), with no Grist form when options.locked;
         - the unattended-sessions toggle;
         - the room directory: count and synced date, a file input dispatching the intercepted upload_rooms action, and a Clear control (clear_rooms).
         For the forms, copy the structure of the Grist API-key modal at settings_window_html.py:1970-2020.
       - Move the Organization Configuration card (renderGeneral, :705-712) from the local settings surface to the admin surface's org_config section. Change its description to "Your own app credentials, and the server and identity-provider settings, from your IT administrator." It keeps dispatching install_org_config, and shows state.error when set.
    2. routes_settings.py: now that no settings surface dispatches install_org_config, remove /api/settings/org_config/upload and its _BESPOKE_SENSITIVE_ROUTE_PATHS entry. Move the remaining settings-path upload tests in test_routes_settings.py to /api/admin/org_config/upload (in test_routes_settings.py or test_routes_admin.py), keeping their assertions.
    3. tests/unit/test_settings_window_html.py:
       - The features section renders each form prefilled from a sample state.
       - A locked grist shows no form.
       - The rooms control dispatches upload_rooms.
       - The local settings surface no longer contains "Organization Configuration", and the admin surface does.
    4. Run ruff check . and python3 -m pytest tests/unit -q. If the hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_browser_smoke.py -q, and fix any smoke test that looked for the Organization Configuration card on /settings so it looks on /admin.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -n "function renderFeatures" src/privacyfence/settings_window_html.py matches
  - grep -rn "/api/settings/org_config/upload" src/privacyfence finds nothing
  - ruff check . passes
- id: p9a-org-settings-becomes-personal
  title: 'Org Settings becomes personal: privacy, stats and the PII gate move to the Admin dispatcher'
  depends_on:
  - p8b-admin-features-and-config
  complexity: M
  touches:
  - src/privacyfence/web/routes_settings.py
  - src/privacyfence/web/org_settings_scope.py
  - src/privacyfence/web/routes_admin.py
  - src/privacyfence/web/server.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/web/test_routes_settings.py
  - tests/unit/web/test_routes_org_settings.py
  - tests/unit/web/test_org_settings_scope.py
  - tests/unit/web/test_routes_admin.py
  - tests/unit/test_settings_window_html.py
  - tests/unit/web/test_server_org_mode.py
  - tests/integration/test_browser_smoke.py
  brief: |-
    Read "Design → D6": the privacy-actions row and paragraph, "Page state" (privacy, stats), and "Rendering" (what the org settings surface loses). After this phase an org admin's /settings is the same as a normal user's. The Admin page renders privacy and stats in p9b.
    1. org_settings_scope.py: change toggle_pii_detection, toggle_pii_category, set_default_policy and set_category_policy to `ActionScope(frozenset({LOCAL_MODE}), admin_only=True)`, matching toggle_calendar_free_busy (:96). In local mode they stay the owner's own settings.
    2. routes_settings.py:
       - Remove those four from _ORG_ALLOWED_ACTIONS and from _apply_org_action.
       - Remove the admin-only privacy and stats data and the PII general fields (_pii_general_fields) from _org_state.
       - GET /settings/privacy in org mode becomes a 302 to /admin.
       - GET /settings always starts on "auto_accept" in org mode.
    3. routes_admin.py:
       - Add the four actions to ADMIN_ACTIONS (org, sensitive, applies "self").
       - Move their _apply_org_action branches and _current_pii_flag here unchanged, keeping the PolicyChangeRejected mapping.
       - Add "privacy" (the rows _org_state built for admins) and "stats" (org_stats.collect(org_config)) to the org admin state.
       - build_org_routes takes install_wide_settings and install_wide_settings_path; pass them from web/server.py.
    4. settings_window_html.py:
       - In org mode the settings surface's privacy and stats sections are false for everyone.
       - renderGeneral's PII card condition (:635-647) becomes `CAPS.mode !== 'org'`.
    5. Tests. Change exactly these, and keep every other assertion:
       - test_org_settings_scope.py:
         - test_the_local_principal_is_never_admin_but_may_still_act_on_its_own_settings (:121) picks from the LOCAL-only admin_only actions.
         - The admin_only parametrizations at :68 and :73 become parameter sets that are not empty, or are dropped if nothing org-routed is admin_only any more (say which in the commit message).
       - test_settings_window_html.py:
         - test_not_applicable_actions_match_org_settings_scope (:968-979) expects the four privacy actions in org not_applicable_actions.
         - test_org_admin_sees_general_and_privacy (:927) and test_stats_page_is_org_admin_only (:963) assert the new layout: no privacy or stats on org settings for anyone.
       - test_routes_org_settings.py:
         - test_admin_sees_privacy_capabilities_non_admin_does_not (:326-347) asserts privacy is false for both.
         - The privacy-action and stats tests (:178-194 and others) move to test_routes_admin.py against /api/admin/..., with the same assertions. The page tests for /settings/privacy (such as :157) assert the 302 to /admin instead.
         - POST /api/settings/toggle_pii_detection in org mode is 404.
       - test_routes_settings.py:449 (test_org_snapshot_carries_the_peek_auth_stub): change its URL to /settings so it keeps checking the stub, and add a separate test that /settings/privacy is a 302 to /admin. test_server_org_mode.py:120: the same 302.
       - tests/integration/test_browser_smoke.py:
         - test_org_settings_page_renders_for_admin (around :2654) asserts the org admin's Settings nav has no Privacy Filter or Stats.
         - TestPhoneLayout's _ADMIN_SECTIONS and the settings-privacy-route case (:2839, :2941-2950) drop privacy and stats from Settings. p9b adds the Admin cases.
    6. Run ruff check . and python3 -m pytest tests/unit -q. If the hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_browser_smoke.py -q. Otherwise state in the phase result that the browser tests were edited but not run here, so the PR's CI run is their check.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -c "from privacyfence.web.routes_settings import _ORG_ALLOWED_ACTIONS as A; assert not {'toggle_pii_detection', 'toggle_pii_category', 'set_default_policy', 'set_category_policy'} & A" exits 0
  - grep -n "toggle_pii_detection" src/privacyfence/web/routes_admin.py matches
  - ruff check . passes
- id: p9b-admin-org-sections-ui
  title: 'The org-only Admin sections: delivery, access, step-up, tokens, audit, privacy and stats'
  depends_on:
  - p9a-org-settings-becomes-personal
  complexity: M
  touches:
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  - tests/integration/test_browser_smoke.py
  brief: |-
    Read "Design → D6": the action table's org rows, the bearer-token rule, "Page state" and "Rendering".
    1. settings_window_html.py, adding to the admin surface in org mode for admins:
       - delivery, access, tokens, audit: one form per section. The fields mirror the action's parameters: number inputs for ints, checkboxes for bools, a select for audit kind and syslog protocol, and textareas for the authz lists. Each posts its whole section through the modal and post() pattern from p8b, prefilled from the state. In audit, the bearer-token variable name is shown read-only with D6's sentence.
       - step_up: only stricter choices. Checkboxes can only go from off to on, and the scope select's options start at the current scope. Add the note "Relaxing step-up is a file edit (ADR 0068)."
       - privacy and stats: move the existing renderPrivacy and renderStats code so the admin surface renders it, without duplicating it. Add the PII Detection Gate toggles (toggle_pii_detection/toggle_pii_category) to the admin privacy section.
       - The restart banner: when state.restart_needed is non-empty, show "Saved. Restart PrivacyFence for these changes to take effect: <labels>." at the top.
    2. tests/unit/test_settings_window_html.py:
       - The org admin surface has all of D6's org sections.
       - Each form renders prefilled from a sample state.
       - The step-up form offers nothing looser than the current value.
       - The bearer-token name has no input.
       - The restart banner renders.
       - No rendered HTML contains a sentinel secret placed in the state.
    3. tests/integration/test_browser_smoke.py: add Admin-page cases to TestPhoneLayout for the org admin sections and the local services and features sections, copying the existing settings cases (docs/testing-policy.md requires phone layout for every app surface). Add an org test that the Admin page renders Privacy and Stats.
    4. Run ruff check . and python3 -m pytest tests/unit -q. If the hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_browser_smoke.py -q. Otherwise say they were not run here.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -n "Restart PrivacyFence for these changes to take effect" src/privacyfence/settings_window_html.py matches
  - grep -n "/admin" tests/integration/test_browser_smoke.py matches
  - ruff check . passes
- id: p10-bundle-tooling
  title: build_org_bundle.py writes only startup settings and credentials; the room sync writes a file to upload
  depends_on:
  - p0-bundle-file-rename
  complexity: M
  touches:
  - scripts/build_org_bundle.py
  - tests/unit/test_build_org_bundle.py
  - scripts/sync_room_directory.py
  - tests/unit/test_sync_room_directory.py
  - tests/unit/test_docs_configuration_reference.py
  brief: |-
    Read "Design → D2" (which keys stay) and "D7". D7 lists every flag exactly.
    1. scripts/build_org_bundle.py:
       - Hide every flag D7 lists with help=argparse.SUPPRESS, and after parsing refuse each one given, with D7's exact message and exit code 2, writing nothing. --idp-step-up-acr-value stays.
       - --merge drops legacy keys with D7's note.
       - Update the "nothing to write" check (:902-910) and --mode local's popping (:770-776) per D7.
       - Keep the signing code byte-identical (tests/unit/test_build_org_bundle.py cross-checks it).
    2. tests/unit/test_build_org_bundle.py:
       - Rewrite the tests that built operational sections so they assert the refusal: one parametrized test named test_moved_flag_is_refused over every moved flag, with the exact message and exit code 2. Pass a valid minimal argument set in each case, so the refusal is what fails.
       - A --merge of a bundle with authz and peek drops them and prints the note.
       - A credentials-only bundle still builds and signs as before.
       - Every signing and cross-check test still passes.
    3. tests/unit/test_docs_configuration_reference.py: _bundle_options() skips actions whose help is argparse.SUPPRESS, per D7. Run it: docs/configuration-reference.md still lists the moved flags at this point, and the test only requires listed options to exist. If it also fails on extra documented flags, stop with status=blocked.
    4. scripts/sync_room_directory.py: --output replaces --org-config per D7. Remove the bundle-merge and signature handling, refuse --org-config with D7's message, write the rooms file with mode 0600, and update the module docstring and example.
    5. tests/unit/test_sync_room_directory.py: rewrite the merge and signing tests into output-file tests (the written file's shape, mode 0600, the --org-config refusal). Keep the directory-fetch tests unchanged.
    6. Run ruff check . and python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/test_sync_room_directory.py tests/unit/test_docs_configuration_reference.py -q.
  acceptance:
  - python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/test_sync_room_directory.py tests/unit/test_docs_configuration_reference.py -q passes
  - python3 -m pytest tests/unit/test_build_org_bundle.py -q -k test_moved_flag_is_refused passes and selects at least 30 tests
  - ruff check . passes
- id: p11-relay-worker
  title: The OAuth redirect relay Worker at oauth.privacyfence.eu and its deploy workflow
  depends_on: []
  complexity: M
  touches:
  - cloudflare/oauth-relay/**
  - .github/workflows/deploy-oauth-relay-worker.yml
  - .github/workflows/dependency-audit.yml
  - .github/dependabot.yml
  - .claude/toolkit.yaml
  - docs/oauth-relay.md
  - docs/README.md
  - scripts/build_site.py
  - docs/downloads-and-release-kpi.md
  - docs/releasing.md
  - .claude/commands/cut-release.md
  brief: |-
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

    Then the release and operations side, per D5's "How the relay is released": write docs/oauth-relay.md (the maintainer runbook with every item D5 lists, including the exact smoke-test curl commands), add it to scripts/build_site.py's CONTRIBUTOR_DOCS and to the contributor half of docs/README.md, add the relay rows to docs/downloads-and-release-kpi.md's Cloudflare resources and credentials tables, and add the health pre-flight line to docs/releasing.md and .claude/commands/cut-release.md. Run python3 -m pytest tests/unit/test_build_site.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q.
    Also implement D9's "The relay on localhost" rules: GET /callback/:grant forwards a loopback host (localhost or 127.0.0.1, optional port) to http://<host>/oauth/callback/<grant>?<query> without the registry, and POST /register still answers 400 host_not_public for it. Test both.
    Also apply D15: SEC-02 (the PSL private-section refusal, generated psl_private.ts from a pinned commit by scripts/update-psl.mjs, tests for x.github.io, a.pages.dev and a.workers.dev; the runbook's review step and revocation turnaround), SEC-03 (per-(host, IP) and per-IP limits, failures never block a later valid proof, an attempts table pruned hourly, no registrations row for unverified hosts), SEC-09 (/registrations needs the maintainer token, 401 otherwise), SEC-10 (the strict parsing rules and every listed negative test), SEC-15 (runbook Logpush/WAF/tail checks), SEC-23 (the NOTIFY send_email binding, src/notify.ts, the daily cap, failure counters on /registrations, and every listed test; the runbook explains the Email Routing setup), SEC-05 and SEC-13 (runbook: required reviewers, one-account note, the built-in secret rotation section). Each rule has a test in the Worker suite.
  acceptance:
  - cd cloudflare/oauth-relay && npm ci && npm run typecheck && npm test && npm run dry-run passes
  - grep -n "oauth.privacyfence.eu" cloudflare/oauth-relay/wrangler.toml matches
  - grep -n "enabled = false" cloudflare/oauth-relay/wrangler.toml matches
  - grep -n "cloudflare/oauth-relay" .claude/toolkit.yaml .github/workflows/dependency-audit.yml .github/dependabot.yml matches in all three
  - python3 -m pytest tests/unit/test_build_site.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q passes
  - grep -n "oauth.privacyfence.eu/health" docs/releasing.md .claude/commands/cut-release.md matches in both
- id: p15-mode-from-env-and-single-user
  title: Mode and server address from the environment, single-user mode on localhost, and a Docker image that starts in org mode
  depends_on:
  - p9b-admin-org-sections-ui
  complexity: M
  touches:
  - src/privacyfence/org_mode.py
  - tests/unit/test_org_mode.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  - src/privacyfence/web/routes_admin.py
  - src/privacyfence/web/server.py
  - tests/unit/web/test_server_org_mode.py
  - src/privacyfence/web/oauth_provider.py
  - tests/unit/web/test_oauth_provider.py
  - tests/conftest.py
  - deploy/docker/Dockerfile
  - deploy/docker/docker-entrypoint.sh
  - deploy/docker/healthcheck.py
  - tests/integration/test_org_docker_image.py
  brief: |-
    Read "Design → D9", except "The relay on localhost", which p5 and p11 already did. Single-user sign-in is p16; after this phase a single-user install starts and serves its pages but nobody can sign in yet, which is expected.
    1. org_mode.py: resolve_mode's environment order and conflict error, the ServerConfig environment fallbacks (including PRIVACYFENCE_TRUSTED_PROXIES), and is_single_user, exactly per D9.
    2. tests/conftest.py: add an autouse fixture that runs monkeypatch.delenv(name, raising=False) for PRIVACYFENCE_MODE, PRIVACYFENCE_BIND_HOST, PRIVACYFENCE_PORT, PRIVACYFENCE_ISSUER_URL and PRIVACYFENCE_TRUSTED_PROXIES, so a developer's environment never leaks into tests.
    3. daemon_main.py load_admin_config: pass single_user=org_mode.is_single_user(bundle) instead of False (D3). routes_admin's apply_change and replace_rooms calls pass single_user the same way, replacing the False p7a and p7b used.
       Then in _start_org_web_server:
       - In single-user mode, replace the "requires an idp section" raise with D9's loopback check and message, and with D9's "a real server never falls back to single-user mode" check (a pinned signing key, or a non-empty imported_from_bundle_sha256).
       - Apply D9's "Types" bullet: OrgAuth.idp and OrgOAuthProvider's idp become Optional, authorize is guarded, routes_org_identity and routes_org_stepup are mounted only when idp is not None (web/server.py:1349, :1373), and the issuer log line (daemon_main.py:1253-1255) handles None.
       - Log D9's single-user warning in run_app.
       - Stop condition: if anything besides those call sites dereferences idp in a way a single-user start reaches, stop with status=blocked and list the call sites.
    4. deploy/docker/Dockerfile, docker-entrypoint.sh and healthcheck.py per D9. tests/integration/test_org_docker_image.py: replace test_refuses_to_start_without_a_bundle (:185) with a test that the image without a bundle starts, is healthy, and logs the single-user line. Copy the existing tests' container-run helper.
    5. Tests:
       - tests/unit/test_org_mode.py: resolve_mode with bundle only, env only, neither, both equal, both different, and an invalid env value; each ServerConfig env fallback, including PRIVACYFENCE_TRUSTED_PROXIES split on commas, with the bundle winning; is_single_user.
         - tests/unit/test_daemon_main.py:
           - an unsigned bundle without mode under PRIVACYFENCE_MODE=org is refused by load_org_config;
           - single-user mode with issuer http://localhost:8765 builds the org app (monkeypatch the server start as the existing org tests do);
           - single-user mode with issuer https://pf.example.com raises D9's message;
           - single-user mode over a data directory with a pinned signing key, or with an imported admin config, raises the "belonged to an organization server" message.
    6. Run ruff check . and python3 -m pytest tests/unit -q.
    7. Push the phase branch, then dispatch build.yml on it (ci.dispatchable; this container cannot build images) and check that the build-org-image job's image tests pass, including the new one.
    Also apply D15 SEC-17 (no 0.0.0.0 or :: in allowed_hosts, web/server.py:1610) and SEC-14's trusted_proxies wildcard refusal, each with a test.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/test_org_mode.py -q -k "resolve_mode or is_single_user or env" passes and selects at least 9 tests
  - the dispatched build.yml run's build-org-image job passes
  - ruff check . passes
- id: p16-single-user-sign-in
  title: Single-user sign-in with a setup code and a passkey, for the browser and for MCP clients
  depends_on:
  - p15-mode-from-env-and-single-user
  complexity: M
  worker_model: opus
  worker_model_reason: This phase replaces the identity provider with its own sign-in (a one-time code, a session cookie and a WebAuthn assertion) and changes the OAuth server's authorize path; a mistake is an authentication bypass that tests may not reveal.
  touches:
  - src/privacyfence/web/single_user_auth.py
  - tests/unit/web/test_single_user_auth.py
  - src/privacyfence/web/oauth_provider.py
  - tests/unit/web/test_oauth_provider.py
  - src/privacyfence/web/routes_mcp.py
  - tests/unit/web/test_routes_mcp.py
  - src/privacyfence/web/server.py
  - tests/unit/web/test_server_org_mode.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  - src/privacyfence/web/org_session.py
  - tests/unit/web/test_org_session.py
  - src/privacyfence/web/routes_security.py
  - tests/unit/web/test_routes_security.py
  - scripts/check_coverage_floor.py
  brief: |-
    Read "Design → D10" in full, and ADR 0177 (consent) and ADR 0178 (finish in the starting session).
    1. web/oauth_provider.py: split handle_idp_callback into _pop_pending, the claims step and _finish_for_principal, with no behaviour change. The existing tests in tests/unit/web/test_oauth_provider.py must pass unchanged before you go on. Then add single_user: bool and an optional idp, the single-user authorize return value, and handle_single_user_continue, per D10.
    2. org_session.set_session_cookie gains `secure: bool = True` per D10, with a test in tests/unit/web/test_org_session.py.
    3. Create web/single_user_auth.py per D10:
       - the setup code: generated and logged in build_routes, normalized comparison, the attempt limit, consumed on success, has_credentials checked on every request;
       - the cookie-bound GET /login (plain_page), POST /login/setup, POST /login/passkey/options and POST /login/passkey/verify;
       - POST /logout and GET /signed-out, as routes_org_identity's;
       - the audit decisions, written the routes_security._audit way;
       - the Secure flag rule.
       Use org_session's existing session-creation and cookie helpers, as routes_org_identity.login_callback does.
    4. routes_security.py: the "Continue" link after enrollment when a safe `next` is present, per D10, with a test in tests/unit/web/test_routes_security.py.
    5. routes_mcp.mount_org_oauth: add the `sessions` keyword, mount /oauth/single-user/continue only for a single-user provider, and mount the IdP callback only for a multi-user one, per D10. Update the call at web/server.py:1335.
    6. web/server.py and daemon_main.py: in single-user mode, mount single_user_auth.build_routes where routes_org_identity's would be, and construct OrgOAuthProvider with single_user=True.
    7. scripts/check_coverage_floor.py: add "src/privacyfence/web/single_user_auth.py" to MODULE_FLOORS at 100. routes_mcp.py already has a floor of 100, so the new route needs full tests.
    8. tests/unit/web/test_single_user_auth.py:
       - The code is logged once and never appears in any response or audit summary.
       - A lowercase code and one without the dash are accepted; a non-ASCII value counts as a wrong attempt and gives no 500.
       - After a successful setup the code no longer works.
       - When a passkey is enrolled and then wiped, the old code does not come back.
       - Cookies are not Secure for an http issuer, and are Secure for an https issuer.
       - The setup redirect carries next through to /security?next=...
       - POST /logout ends the session and redirects to /signed-out.
       - A right code gives a session for single-user and redirects to /security.
       - A wrong code gives the message; the fifth wrong code uses the code up, and even the right code is then refused.
       - A missing or mismatched pf_setup_binding cookie is refused, as is a foreign Origin.
       - After enrollment the setup form is gone and POST /login/setup is refused.
       - The passkey options and verify flow: monkeypatch webauthn_stepup.verify_assertion to succeed or fail, giving a session or a refusal.
       - next is sanitized: //evil.example is refused.
       - Each audit decision is recorded.
    9. tests/unit/web/test_oauth_provider.py and test_routes_mcp.py:
       - A single-user authorize returns the /login?next=/oauth/single-user/continue... URL.
       - /oauth/single-user/continue without a session goes to /login, with a session finishes through consent exactly like the IdP path (copy the IdP-path consent tests), and refuses a state that is unknown or expired.
       - A multi-user server has no /oauth/single-user/continue route, and a single-user server has no IdP callback route.
    10. tests/unit/web/test_server_org_mode.py: in single-user mode /login is the single-user sign-in page and /oauth/idp/login-callback does not exist.
    11. Run ruff check . and python3 -m pytest tests/unit -q. If the hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_browser_smoke.py -q.
    Stop condition: if session creation in routes_org_identity cannot be reused without copying private code that carries security checks (for example session fixation handling), stop with status=blocked and describe what would have to be duplicated.
    Also apply D15 SEC-16 (check and consume under one lock, with a concurrency test) and SEC-01's tests: a valid single-user session cookie without an assertion cannot approve or perform a sensitive Admin action.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/web/test_single_user_auth.py -q --cov=privacyfence.web.single_user_auth --cov-branch --cov-report=term-missing passes and reports 100%
  - python3 -m pytest tests/unit/web/test_oauth_provider.py -q passes
  - ruff check . passes
- id: p17-local-mode-deprecation
  title: 'Local mode shows that it is deprecated: the log, privacyfence_status, a page strip and a one-time companion notice'
  depends_on:
  - p16-single-user-sign-in
  complexity: M
  touches:
  - src/privacyfence/org_mode.py
  - src/privacyfence/daemon_main.py
  - src/privacyfence/web/mcp_dispatch.py
  - src/privacyfence/web/mcp_tools.py
  - docs/tools-reference.md
  - src/privacyfence/web_shell.py
  - src/privacyfence/web/routes_settings.py
  - src/privacyfence/web/routes_approvals.py
  - src/privacyfence/web/routes_security.py
  - src/privacyfence/web/routes_admin.py
  - src/privacyfence/companion.py
  - tests/unit/test_org_mode.py
  - tests/unit/test_daemon_main.py
  - tests/unit/web/test_mcp_dispatch.py
  - tests/unit/test_web_shell.py
  - tests/unit/test_companion.py
  - tests/unit/web/test_routes_settings.py
  brief: |-
    Read "Design → D11".
    1. org_mode.py: add LOCAL_MODE_DEPRECATION with the exact text. daemon_main.run_app logs it at WARNING in local mode.
    2. web/mcp_dispatch.py: add the "deprecation" field where `result` is built (:506-508), so all three return paths carry it. web/mcp_tools.py: extend the privacyfence_status description per D11. If a test pins docs/tools-reference.md to the generator's output, run python3 scripts/generate_tools_reference.py and commit the result.
    3. web_shell.py: add the deprecation_html keyword and strip per D11, styled with the existing notice strip's CSS. Pass it from every local-mode wrap call site: grep `web_shell.wrap(` and `wrap(` in src/privacyfence/web; the local sites are those using the default or local nav items.
    4. companion.py: add _notice_state_path() and the one-time notice in _run_tray (after the icon starts, on the UI thread, as _notify does) and in _run_serve (_notify_send), per D11.
    5. Tests:
       - test_org_mode.py: the constant contains "PrivacyFence 8".
       - test_daemon_main.py: local start logs it, org start does not.
       - test_mcp_dispatch.py: local status has deprecation equal to the constant; org status has null.
       - test_web_shell.py: the strip renders with deprecation_html and not without it.
       - test_routes_settings.py: the local /settings page contains the strip.
       - test_companion.py, with monkeypatched paths: the notice shows once and writes the marker; a second start does not show it; an unwritable path still shows it and raises nothing.
    6. Run ruff check . and python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -n "LOCAL_MODE_DEPRECATION" src/privacyfence/org_mode.py src/privacyfence/web/mcp_dispatch.py src/privacyfence/daemon_main.py matches in each
  - ruff check . passes
- id: p18-moved-setting-strings
  title: Messages that pointed at the bundle for moved settings point at the Admin page
  depends_on:
  - p17-local-mode-deprecation
  complexity: S
  touches:
  - src/privacyfence/web/mcp_dispatch.py
  - tests/unit/web/test_mcp_dispatch.py
  - src/privacyfence/web/routes_settings.py
  - tests/unit/web/test_routes_settings.py
  - src/privacyfence/settings_controller.py
  - tests/unit/test_settings_controller.py
  - sync/src/privacyfence_sync/runner.py
  - src/privacyfence/scoped_tokens.py
  - tests/unit/test_scoped_tokens.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  - src/privacyfence/resources/settings.yaml.example
  brief: |-
    Replace these strings exactly, and update every test that pins the old text (grep each old phrase in tests/ and sync/tests/):
    1. web/mcp_dispatch.py:651-653 (begin_unattended_session) becomes: "Unattended sessions are disabled. An administrator turns them on on the Admin page (Features) before this connection can be marked unattended."
    2. web/routes_settings.py _SCOPED_TOKENS_DISABLED (:1429-1432), and the identical text in scoped_tokens.py:518, become: "Scoped tokens are not enabled on this install. Your administrator turns them on on the Admin page."
    3. settings_controller.py:1299 becomes: f"{connector_label(connector)} isn't offered on this install. Turn it on on the Admin page (Services)."
    4. sync/src/privacyfence_sync/runner.py _UNATTENDED_DISABLED (:39-42) becomes: "This PrivacyFence does not allow unattended sessions (Admin page, Features). Ask your administrator, or run with --attended." sync/tests/test_preflight.py compares against the constant, so it needs no change.
    5. settings_window_html.py, the welcome banner (:747-751): replace the sentence "If your IT team gave you an organization config bundle, install it first from the General page; only the connectors it enables are listed." with "The Admin page decides which services are offered." Update tests/unit/test_settings_window_html.py:263's expected phrase to "Admin page".
    6. resources/settings.yaml.example:176: change the comment to say unattended sessions are set on the Admin page.
    7. Run ruff check ., python3 -m pytest tests/unit -q, and, per the sync/** rule in .claude/toolkit.yaml: pip install -e "./sync[test]", then pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-report=term-missing --cov-fail-under=100, mypy sync/src and bandit -r sync/src.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -rnE "in the organization config bundle|install it first from the General page|isn't enabled in your organization config" src/privacyfence finds nothing
  - pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100 passes
  - ruff check . passes
- id: p19a-migration-checkpoint
  title: Every 7.x start migrates every person's data and records the data-format marker
  depends_on:
  - p18-moved-setting-strings
  complexity: M
  touches:
  - src/privacyfence/data_format.py
  - tests/unit/test_data_format.py
  - src/privacyfence/policy_upgrade.py
  - tests/unit/test_policy_upgrade.py
  - src/privacyfence/scoped_tokens.py
  - tests/unit/test_scoped_tokens.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  - tests/conftest.py
  - scripts/check_coverage_floor.py
  brief: |-
    Read "Design → D12" in full. Every step, problem text and rule is there.
    1. policy_upgrade.py: add ensure_canonical_verbs(config, resolved, principal) -> list[str] per D12 step 4, for settings.yaml only. It edits only `verbs` values, on version-3 sections only, keeps every other key, backs up once, and writes with atomic_write_text.
    2. scoped_tokens.py: add canonicalize_verbs_in_file(path) -> tuple[bool, list[str]] next to own_rules_in_file, per D12. It edits only `verbs` inside each entry's rules, leaves legacy entries, unknown keys and unreadable rows untouched, and writes with atomic_write_json only on change.
    3. Create src/privacyfence/data_format.py with DATA_FORMAT, marker_path, read_marker, check_not_newer, migrate_everything, status and reset_for_tests, per D12. Use D12's steps, order and exact problem texts. It does its own v1 clean-up write, re-reads the file after ensure_scope_rows, turns every per-file error into a problem, and imports daemon_main helpers inside the function.
    4. daemon_main.py:
       - main() calls data_format.check_not_newer(data_format.read_marker()) right after check_runtime_identity (~2487); a ConfigurationError goes to _refuse_to_start.
       - run_app calls data_format.migrate_everything(log=logger.warning) after load_admin_config, in both modes, before the web server is built.
       - Remove _upgrade_all_principals and its call in _start_org_web_server (~1231). Keep load_config's own lazy calls; they are idempotent.
    5. tests/conftest.py: patch data_format.marker_path in the D8 fixture, exempt test_data_format, and call data_format.reset_for_tests() in _reset().
    6. tests/unit/test_daemon_main.py: in the run_app harness (_patch_common, ~3257-3301), stub data_format.migrate_everything (returning []) and data_format.check_not_newer, so run_app tests never walk the source checkout.
    7. scripts/check_coverage_floor.py: add "src/privacyfence/data_format.py" to MODULE_FLOORS at 100.
    8. Tests:
       - tests/unit/test_data_format.py, with paths.data_dir patched to tmp_path, and the admin config created where a test expects the marker:
         - A fresh data dir gets the marker with format 7 and the running version.
         - A v2 section is converted and the marker is written.
         - A v2 section whose rewrite fails (monkeypatch atomic_write_text to raise for that path) gives the "still in the version-2 format" problem and no marker.
         - A marked v1 section is cleaned and the marker is written; a failed clean-up write gives the problem; an unmarked v1 section gives the problem and no marker.
         - A token entry with rule_ids and no rules gives the problem.
         - A row with a legacy verb name gets only its verbs rewritten. Assert every other key, including former_ids, conditions/predicate and an unknown extra key, is unchanged, and that a backup exists. The same for a token's rules row.
         - A row already canonical, but with operations in a hand-made order, is not rewritten (the file's mtime and content are unchanged).
         - An unreadable row gives the problem and is left as it is.
         - A corrupt settings.yaml and a corrupt scoped_tokens.json of an os-… principal each give a problem; migrate_everything does not raise, and other principals are still migrated.
         - An org principal under users/<id> that never signed in is migrated.
         - A marker at format 7 is not rewritten.
         - check_not_newer with format 8 raises the exact message.
         - A malformed marker reads as None.
         - status() reflects the last run.
       - tests/unit/test_policy_upgrade.py: ensure_canonical_verbs; no write when already canonical. tests/unit/test_scoped_tokens.py: canonicalize_verbs_in_file.
       - tests/unit/test_daemon_main.py:
         - main() refuses a format-8 marker before load_config runs (assert load_config was not called).
         - Rewrite test_upgrade_all_principals_runs_before_the_token_store_loads (~2440-2483) to record that run_app calls migrate_everything before _maybe_start_web_server.
         - Port the three direct _upgrade_all_principals tests (~2485-2546) to migrate_everything, keeping their assertions.
    9. Run ruff check . and python3 -m pytest tests/unit -q.
    Also apply D15 SEC-21: both backup files are written mode 0600 through secure_files, with a test.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/test_data_format.py -q --cov=privacyfence.data_format --cov-branch --cov-report=term-missing passes and reports 100%
  - grep -n "_upgrade_all_principals" src/privacyfence/daemon_main.py finds nothing
  - ruff check . passes
- id: p19b-upgrade-readiness-on-admin
  title: The Admin page says whether the install is ready for PrivacyFence 8
  depends_on:
  - p19a-migration-checkpoint
  complexity: S
  touches:
  - src/privacyfence/web/routes_admin.py
  - tests/unit/web/test_routes_admin.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  brief: |-
    Read "Design → D12" ("Readiness on the Admin page").
    1. routes_admin.py: add "upgrade_ready": data_format.status() to the admin page state in both modes.
    2. settings_window_html.py: render D12's two lines at the bottom of the admin surface, showing at most the first three problems, each passed through esc() (settings_window_html.py:379).
    3. Tests:
       - test_routes_admin.py: the state carries upgrade_ready in both modes.
       - test_settings_window_html.py: both lines are in the page's JS, and the problem text goes through esc(). Rendering is client-side, so assert on the JS source; if the hook's Chromium line had no WARNING, also add a browser-smoke check that a problem containing "<b>" shows as text.
    4. Run ruff check . and python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -n "Ready for PrivacyFence 8" src/privacyfence/settings_window_html.py matches
  - ruff check . passes
- id: p20a-idp-from-environment
  title: An organization server can take its IdP from environment variables, with no bundle
  depends_on:
  - p19b-upgrade-readiness-on-admin
  - p10-bundle-tooling
  complexity: M
  touches:
  - src/privacyfence/org_mode.py
  - tests/unit/test_org_mode.py
  - src/privacyfence/org_identity.py
  - tests/unit/test_org_identity.py
  - src/privacyfence/admin_config.py
  - tests/unit/test_admin_config.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  - tests/conftest.py
  - deploy/docker/compose.yaml
  - deploy/docker/privacyfence.env.example
  - deploy/docker/docker-entrypoint.sh
  - tests/integration/test_org_docker_image.py
  brief: |-
    Read "Design → D14" ("The IdP from the environment", "Signing", "A server stays a server", "Docker") and D9.
    1. org_mode.py: add a module logger, idp_section(org_config) per D14 (the variable table, the _FILE rule, the both-set error, the partial-environment error and the once-only warning, which names PRIVACYFENCE_ADMIN_EMAILS too), and the secret-free _idp_present predicate. Change is_single_user to use _idp_present.
    2. org_identity.py: IdpConfig.from_org_config reads org_mode.idp_section(org_config) instead of org_config.get("idp") (:118). Make client_secret field(repr=False) (:85).
    3. admin_config.py: add the "org_server" flag (schema default false; load() normalizes it), and keep apply_change from ever changing it. daemon_main.py: set it to true and save on the first start for which _idp_present is true, before IdP discovery. D9's single-user refusal checks it.
    4. tests/conftest.py: extend the environment-clearing fixture from p15 to every PRIVACYFENCE_IDP_* variable and PRIVACYFENCE_ADMIN_EMAILS, and reset org_mode's once-only warning flag in _reset().
    5. deploy/docker: privacyfence.env.example, the compose.yaml secrets example and the entrypoint message, per D14. tests/integration/test_org_docker_image.py: copy TestRunningContainer (:206) into a positive test. Start the image with no bundle and -e PRIVACYFENCE_IDP_ISSUER=<the mock IdP's base_url>, PRIVACYFENCE_IDP_CLIENT_ID, PRIVACYFENCE_ISSUER_URL=https://<ISSUER_HOST>, PRIVACYFENCE_PORT and DEV_ALLOW_INSECURE_IDP=1, as that class's bundle-based setup does. Assert the entrypoint's environment line, that /login redirects to the mock IdP, and that no single-user line is logged.
    6. Tests:
       - test_org_mode.py:
         - idp_section with a bundle only; env only; both (the bundle wins, one warning, also triggered by PRIVACYFENCE_ADMIN_EMAILS alone);
         - the secret from an env value and from a file; both secret forms set is an error; an unreadable file names the variable and not the content;
         - a partial environment (CLIENT_ID missing, or only ADMIN_EMAILS set) is an error;
         - the comma lists;
         - is_single_user with an env idp is false, and it never opens the secret file (monkeypatch open to fail for it).
       - test_org_identity.py: from_org_config builds from env (monkeypatch discover_idp); repr(IdpConfig(...)) does not contain the secret.
       - test_daemon_main.py: org mode with no bundle and env idp starts the org app (monkeypatch discovery and the server start as the existing tests do); org_server is set even when discovery fails; a later start with the IdP variables removed and a localhost issuer is refused with D9's message; an unsigned bundle in org mode is still refused.
       - test_admin_config.py: apply_change cannot change org_server.
       - No log record (caplog) contains the secret in any test.
    7. Run ruff check . and python3 -m pytest tests/unit -q. Push, then dispatch build.yml on the phase branch (ci.dispatchable) and check that the build-org-image job's image tests pass.
    Also apply D15 SEC-14 (a signed bundle without idp refuses the environment IdP unless idp.from_environment is true) and SEC-11's rule that a re-import keeps org_server, each with tests.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/test_org_mode.py -q -k idp_section passes and selects at least 7 tests
  - the dispatched build.yml run's build-org-image job passes
  - ruff check . passes
- id: p20b-admins-by-email
  title: Admins can be named by IdP-verified email
  depends_on:
  - p20a-idp-from-environment
  complexity: S
  touches:
  - src/privacyfence/org_identity.py
  - tests/unit/test_org_identity.py
  - scripts/build_org_bundle.py
  - tests/unit/test_build_org_bundle.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  brief: |-
    Read "Design → D14" ("Admins by email").
    1. org_identity.py: IdpConfig gains admin_emails (each .strip().casefold(), from the section's admin_emails list). principal_from_claims sets is_admin when the group rule matches, or when isinstance(claims.get("email"), str), its .strip().casefold() is in admin_emails, and claims.get("email_verified") is True. The three callers (oauth_provider.py:470, routes_org_identity.py:222, routes_org_stepup.py:179) all pass verified ID-token claims and need no change.
    2. scripts/build_org_bundle.py: add --idp-admin-email (repeatable, action="append"), writing idp.admin_emails, next to --idp-admin-group-value. tests/unit/test_build_org_bundle.py: the flag writes the list and the bundle still signs.
    3. tests/unit/test_org_identity.py:
       - A verified listed email is admin; matching is case-insensitive.
       - email_verified false, missing, or the string "true" is not admin.
       - An unlisted email is not admin.
       - A list-valued email claim is not admin.
       - A configured email with surrounding whitespace or different case still matches.
       - The group rule still works alone and together with the email list.
    4. Run ruff check . and python3 -m pytest tests/unit -q.
    Also apply D15 SEC-07 (the Google hd rule, email_verified in check_authz_policy, ASCII lower with non-ASCII refused), SEC-08 (reserved subs hashed; the startup log line) and SEC-14's --idp-from-environment flag in build_org_bundle.py, each with tests. Add the SEC-07 behaviour change to the commit message so p13b can note it in the CHANGELOG.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/test_org_identity.py -q -k admin passes
  - ruff check . passes
- id: p12-adrs
  title: ADRs 0239-0249 and the status lines on the ADRs they amend
  depends_on:
  - p20b-admins-by-email
  - p10-bundle-tooling
  - p11-relay-worker
  complexity: M
  touches:
  - docs/adr/0239-built-in-oauth-apps-ship-in-every-distribution.md
  - docs/adr/0240-org-config-holds-only-startup-settings-and-app-credentials.md
  - docs/adr/0241-operational-settings-live-in-a-daemon-written-admin-config.md
  - docs/adr/0242-administration-is-a-separate-admin-page.md
  - docs/adr/0243-org-servers-use-built-in-apps-through-a-domain-verified-relay.md
  - docs/adr/0244-organization-mode-can-start-from-the-environment-and-run-single-user-on-localhost.md
  - docs/adr/0245-local-mode-is-deprecated-in-7-and-removed-in-8.md
  - docs/adr/0246-7x-is-the-migration-checkpoint-for-8x.md
  - docs/adr/0247-the-6-to-7-upgrade-is-reversible.md
  - docs/adr/0248-an-organization-server-can-start-from-environment-variables.md
  - docs/adr/0249-admins-can-be-named-by-verified-email.md
  - docs/adr/0011-org-mode-runs-its-own-oauth-authorization-server.md
  - docs/adr/0200-an-organization-bundle-carries-a-signed-serial-and-an-older-one-is-refused.md
  - docs/adr/README.md
  - docs/adr/0016-org-config-bundle-hash-log-and-signing.md
  - docs/adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md
  - docs/adr/0217-the-org-admin-stats-page-shows-counts-only.md
  brief: |-
    1. Check `ls docs/adr | tail -3` first. If 0239-0249 are taken by ADRs merged to main since this plan was written, take the next free numbers and use them everywhere; p13a-c read the file names from docs/adr/.
    2. Write the eleven ADRs in docs/adr/README.md's template: Status (Accepted, today's date, Implemented), Context, Decision, Alternatives considered, Consequences, Verification, Related.
       - Their decisions are this plan's "ADRs" section. Context, alternatives and consequences come from the Goal, Design D1-D8, "Accepted risks" and "Risks".
       - Alternatives to record:
         - 0239: fetch the credentials from a PrivacyFence endpoint at runtime; let the relay broker the token exchange and hold the secrets.
         - 0240: keep everything in the signed bundle; a third startup-only file for mode/server/idp; let the daemon write the org's own credentials into an unsigned org_config.json.
         - 0241: refuse to start with legacy keys; keep them as read-only defaults alongside the admin config; keep switches in the bundle for desktop installs.
         - 0242: keep admin sections inside Settings.
         - 0243: no relay; a maintainer-approved list of servers; forwarding to any server that asks.
         - 0244: single-user mode that still needs an IdP; signing in with Google through the relay; a fixed password.
         - 0245: dropping local mode in 7 without a deprecation release; keeping local mode indefinitely.
         - 0246: keeping every converter forever; a per-file version check in 8 instead of one marker; migrating lazily per principal as before.
         - 0248: keeping a signed bundle mandatory for org mode; reading every bundle key from the environment (only the IdP and server need it); mixing bundle and environment fields within the idp section.
         - 0249: the group claim only; any email claim without email_verified; the first person to sign in becomes admin.
         - 0247: renaming org_config.json in place (breaks a downgrade); sharing the pin files (a 7.x serial would refuse 6.x's older bundle); reading whichever file is newer.
       - Fold in D15 where it names an ADR: 0239 (SEC-02 threat, SEC-13 rotation, SEC-22), 0241 (SEC-04 passkey for every sensitive Admin action, SEC-20), 0243 (SEC-05 one account and required reviewers, the Slack/Miro residual risk, SEC-09, SEC-15 wording, SEC-23 notification emails in place of a waiting period), 0244 (SEC-01: single-user mode is no protection against an agent that can run docker), 0246 (SEC-21), 0248 (SEC-14).
       - Link source files, never this plan. Do not mention any model or agent.
    3. Add one Status line to each amended ADR, and nothing else:
       - 0016: "Amended by [0240](...): the signature no longer covers operational settings. Amended by [0247](...): the bundle and pin file names."
       - 0200: "Amended by [0247](...): the serial pin file name."
       - 0011: "Amended by [0248](...): the IdP may come from environment variables."
       - 0016's line also names 0248: "org mode no longer always needs a signed bundle".
       - 0196: "Amended by [0240](...) and [0241](...)."
       - 0217: "Amended by [0242](...): the Stats section is on the Admin page."
    4. docs/adr/README.md: add eleven index rows in the existing format:
       - 0239: "Accepted; relates to 0040"
       - 0240: "Accepted; amends 0016, 0196"
       - 0241: "Accepted; amends 0196; relates to 0016, 0034, 0068, 0070, 0200"
       - 0242: "Accepted; amends 0217; relates to 0033"
       - 0243: "Accepted; relates to 0011, 0186"
       - 0244: "Accepted; relates to 0011, 0177, 0213"
       - 0245: "Accepted; relates to 0002, 0003"
       - 0246: "Accepted; relates to 0041, 0047, 0171, 0172, 0228, 0236"
       - 0247: "Accepted; amends 0016, 0200; relates to 0246"
       - 0248: "Accepted; amends 0011, 0016"
       - 0249: "Accepted; relates to 0011"
    5. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q.
  acceptance:
  - ls docs/adr/0239-*.md docs/adr/0240-*.md docs/adr/0241-*.md docs/adr/0242-*.md docs/adr/0243-*.md docs/adr/0244-*.md docs/adr/0245-*.md docs/adr/0246-*.md docs/adr/0247-*.md docs/adr/0248-*.md docs/adr/0249-*.md lists eleven files (or the renumbered eleven)
  - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q passes
  - git diff origin/main --stat -- docs/adr/0040-telegram-app-credentials-ship-in-every-distribution.md prints nothing
- id: p13a-docs-operator
  title: 'Operator docs: the organization deployment guide, Docker and the configuration reference'
  depends_on:
  - p12-adrs
  complexity: M
  touches:
  - docs/org-mode-setup-guide.md
  - docs/org-mode-docker.md
  - docs/configuration-reference.md
  - docs/how-it-works.md
  - docs/connector-qa.md
  - docs/testing-policy.md
  brief: |-
    Reference docs describe today's behaviour and link the ADRs p12 wrote (take their file names from docs/adr/). Wherever a doc names the file 7.x reads or installs, use org/org_bundle.json (D13); org_config.json appears only as the 6.x name.
    1. org-mode-setup-guide.md:
       - §5 "Connector apps": built-in apps through the relay are the default. Give the relay requirements from D5 (an https issuer_url with no path, a server reachable from the internet at /.well-known/privacyfence-relay, allowed through §7's IP restriction, outbound HTTPS to oauth.privacyfence.eu), the 35-day re-verification, the public /registrations list, and that your own app goes in the bundle.
       - §6 "Build the bundle": only the startup and credential flags remain; list the moved flags and say they are now on the Admin page; describe the one-time import on upgrade and the "are ignored" warning.
       - "Who may sign in", §10 (install-wide policy), §12 (step-up, including "stricter only from the page; relax by editing org/admin_config.json and restarting"), "Notifications on a phone", §13 (file delivery), audit forwarding and scoped tokens: each now points to the matching Admin page section and says which changes need a restart.
       - A new "Admin page" subsection: who sees it, the sections, step-up.
       - §4 "Identity provider" and "Who is an admin": the environment alternative to the bundle (D14), admins by verified email, that Entra ID admins use the group claim, and that a multi-tenant issuer like accounts.google.com should be paired with allowed domains (Admin → Sign-in) or an Internal consent screen. Note that one Google OAuth client can also serve Peek and Grist sign-in, but a separate client per service is recommended. §6: the bundle is optional when the IdP comes from the environment.
       - §14 "Upgrade" and a new "Going back to 6.x" subsection: 7.x copies org_config.json and its pin files to org_bundle.json and the new pin names, and never changes 6.x's files (D13). To go back, stop 7.x and install the latest 6.x (6.9.x): it runs with org_config.json as it was, and changes made on the Admin page do not carry back. To re-upgrade from 6.x's current files, delete D13's list of 7.x files first. Use the new file name everywhere the guide installs or copies a bundle (Docker: org/org_bundle.json).
       - Also in §14: upgrading to 8.x requires having run 7.x at least once with "Ready for PrivacyFence 8: yes" on the Admin page. Explain data_format.json (D12), the problems it can report and how to fix each, and that 7.x refuses data from a newer release.
       - §15 Troubleshooting: rows for the relay errors (proof_failed, host_not_public, revoked, too_soon, the issuer_url message, and the relay's 400 text from D5) and for the "are ignored" warning.
    2. org-mode-docker.md, leading with a "Quick start" per D14:
       - DNS;
       - register one OIDC app with the three redirect URIs (link org-mode-setup-guide.md §4);
       - fill privacyfence.env from privacyfence.env.example, with the secret as a Docker secret file;
       - set PF_HOSTNAME in .env;
       - docker compose up -d;
       - sign in as a listed admin email (or group);
       - Admin → Services → offer.
       Keep the bundle flow as the option for your own connector apps or a signed configuration. Also:
       - Outbound HTTPS to oauth.privacyfence.eu when using built-in apps.
       - admin_config.json lives in the data volume and is part of the backup.
       - The bundle is credentials-only.
       - A new first section, "Run it on this computer" (open it at http://localhost:8765, not 127.0.0.1, because the Host allowlist and the passkey's rp_id are localhost): the D9 docker run command with the 127.0.0.1 publish and why it matters; the setup code in `docker logs`; passkey enrollment; connecting a service through the relay; adding http://localhost:8765/mcp to an AI client; the lost-passkey recovery from D10; and how to go from single-user mode to a multi-user server (PRIVACYFENCE_IDP_* and a public PRIVACYFENCE_ISSUER_URL in privacyfence.env, or an idp section in a signed bundle).
       - The PRIVACYFENCE_MODE, PRIVACYFENCE_BIND_HOST, PRIVACYFENCE_PORT and PRIVACYFENCE_ISSUER_URL variables.
       - D15 SEC-01's warning, prominently in "Run it on this computer": anyone who can run docker on that computer controls the install; use a separate OS account (rootless Docker) or a VM, or a server. Also SEC-16's note that the setup code stops working once a passkey is enrolled.
    3. configuration-reference.md (and the key-rotation text in org-mode-setup-guide.md): after the upgrade, rotation deletes the new-name pins; the legacy ones only when going back to 6.x is no longer wanted (D15 SEC-12). Also the D9 and D14 environment variables and their precedence (a bundle's idp section wins over PRIVACYFENCE_IDP_*); org_bundle.json's keys (D2), and that it is the 6.x org_config.json copied once (D13); org/admin_config.json (D3 schema; written by the Admin page; hand-edit only to relax step-up, then restart); org/relay_token.txt; the PRIVACYFENCE_BUILTIN_APPS development variable and its JSON shape.
    4. configuration-reference.md: remove the moved build_org_bundle.py flags from its option list (p10 made the reference test skip refused flags) and add a short "Refused since 7.0" note naming them. Say that step_up.rp_id, rp_name and batch have no Admin control and are set by editing org/admin_config.json, then restarting.
    5. how-it-works.md (:277-278), connector-qa.md (:153): where they say operational settings live in the bundle, point to the Admin page and admin_config.json instead. testing-policy.md (:183): add the Admin page to the phone-layout list of app surfaces.
    6. Run the docs tests below, python3 -m pytest tests/unit/test_docs_configuration_reference.py -q, and ruff check ..
  acceptance:
  - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_pages.py -q passes
  - python3 -m pytest tests/unit/test_docs_configuration_reference.py -q passes
  - grep -n "oauth.privacyfence.eu" docs/org-mode-setup-guide.md docs/org-mode-docker.md matches in both
  - grep -n "admin_config.json" docs/configuration-reference.md docs/org-mode-setup-guide.md matches in both
- id: p13b-docs-user
  title: User docs, provider setup docs, security notes and the changelog
  depends_on:
  - p12-adrs
  complexity: M
  touches:
  - docs/getting-started.md
  - docs/connecting-a-service.md
  - docs/approvals-and-policy.md
  - docs/google-cloud-setup.md
  - docs/slack-setup.md
  - docs/salesforce-setup.md
  - docs/atlassian-setup.md
  - docs/miro-setup.md
  - docs/grist-setup.md
  - docs/peek-setup.md
  - docs/telegram-setup.md
  - docs/security-and-compliance.md
  - docs/moving-to-organization-mode.md
  - docs/install-macos.md
  - docs/install-windows.md
  - docs/install-linux.md
  - docs/README.md
  - docs/privacyfence-sync.md
  - sync/README.md
  - plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md
  - CHANGELOG.md
  brief: |-
    Reference docs describe today's behaviour and link the ADRs p12 wrote. Wherever a doc names the file 7.x reads or installs, use org/org_bundle.json (D13).
    1. getting-started.md and connecting-a-service.md:
       - A fresh install offers Google, Slack, Salesforce, Atlassian, Miro and Telegram with built-in apps and no bundle.
       - The Admin page: who sees it; Services and Features; on desktop it is the install owner's.
       - The bundle is now only for your own apps (and org mode's server and IdP).
       - Changing a service's app makes people reconnect.
    2. approvals-and-policy.md: in org mode the install-wide privacy policy is edited on the Admin page; Settings is personal.
    3. Provider setup docs (google-cloud, slack, salesforce, atlassian, miro):
       - Open with "You need this page only to use your organization's own app instead of the built-in one."
       - Replace any advice to trust or pre-approve the client ID with D15 SEC-02's exact sentence; never tell anyone to trust the built-in app.
       - Give the provider caveat:
         - Google: an unverified-app screen and a 100-user cap until verification, which includes an annual security assessment for Gmail and Drive scopes; Workspace admins can trust the client ID; an org-mode override must be a Web application client.
         - Salesforce: orgs that block uninstalled connected apps need an admin to install or approve it.
         - Slack: workspaces that require app approval need an admin's approval once.
         - Atlassian and Miro: nothing extra.
       - Bundle flags only for credentials.
       - grist-setup.md and peek-setup.md: the server address is set on the Admin page (Features); Grist's OAuth app still goes in the bundle.
       - telegram-setup.md: the Admin page switch.
       - google-cloud-setup.md "Room directory sync": the script writes rooms.json (--output) and you upload it on the Admin page.
    4. security-and-compliance.md, a "Built-in apps, the admin config and the relay" subsection:
       - Built-in secrets are public in effect (0239), and secret rotation (D15 SEC-13).
       - Rewrite :19-21 and :66-67 per D15 SEC-01, and state per provider what a compromised relay could do (D15 SEC-05).
       - What the relay sees and does not see (0243).
       - The admin config's settings.yaml-level trust, and that signing now covers only startup settings and credentials (0240, 0241).
    5. Create docs/moving-to-organization-mode.md, the page LOCAL_MODE_DEPRECATION links to (privacyfence.eu/docs/moving-to-organization-mode/):
       - why desktop mode goes away in 8 (ADR 0245);
       - the two ways forward: the Docker image in single-user mode on this computer (link org-mode-docker.md "Run it on this computer") and an organization server (org-mode-setup-guide.md);
       - bringing rules along (link org-mode-setup-guide.md "Moving a person from a desktop install");
       - before any later upgrade to 8.x, start 7.x once and check "Ready for PrivacyFence 8" (D12).
       Add it to docs/README.md's index in the existing format. Add a one-paragraph deprecation note at the top of install-macos.md, install-windows.md and install-linux.md linking to it, and to getting-started.md's "Local or organization deployment".
       If the website docs build has a list of published docs, find where test_website_docs_pages.py reads it; if that list needs the new page, add it there.
    6. docs/privacyfence-sync.md (:50-51) and sync/README.md (:89-94): unattended sessions are turned on on the Admin page. Copy sync/README.md to plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md, as the sync/** rule in .claude/toolkit.yaml requires.
       Also say, in connecting-a-service.md, that a bundle adding a provider's credentials later still needs the service switched on on the Admin page.
    7. CHANGELOG.md under "## [Unreleased]":
       - one entry each for: built-in apps; the Admin page and personal-only Settings; the org config split with the one-time import (naming the moved build_org_bundle.py flags and sync_room_directory.py's --output); and organization servers using built-in apps through the relay;
       - a "Removed" or "Changed" note that build_org_bundle.py refuses the moved flags;
       - the Docker image's single-user mode on localhost and the environment variables;
       - "Deprecated: desktop (local) mode, to be removed in PrivacyFence 8";
       - "Every start upgrades all stored data and records data_format.json. PrivacyFence 8 will require it, so upgrade to 8 only from 7.x.";
       - "PrivacyFence 7 reads org/org_bundle.json, copied once from org_config.json, and never changes 6.x's files, so you can go back to 6.9.x" (D13);
       - a line at the top of the Unreleased section: "Breaking changes; this release is 7.0.0."
       - No version heading.
    8. Run the docs tests below and ruff check ..
  acceptance:
  - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_pages.py -q passes
  - grep -n "Admin page" docs/connecting-a-service.md docs/slack-setup.md docs/grist-setup.md matches in each
  - grep -n "moving-to-organization-mode" docs/README.md docs/install-macos.md docs/install-windows.md docs/install-linux.md matches in each
  - git diff origin/main -- CHANGELOG.md | grep -c "^+## \[" prints 0
- id: p13c-website
  title: Website pages and privacy policy
  depends_on:
  - p12-adrs
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
  brief: |-
    1. website/privacy/index.html:
       - Extend the scope sentence (lines ~26-27) to name oauth.privacyfence.eu.
       - Add a section "Sign-in relay (oauth.privacyfence.eu)" after the download section. It says the relay:
         - is a small Cloudflare Workers service that forwards an organization server's sign-in redirect;
         - processes the IP address and the request in transit;
         - stores only each registered server's host name and registration dates, visible only to PrivacyFence maintainers;
         - writes no logs (D15 SEC-15);
         - emails the PrivacyFence maintainers the host name, the IP address and the country of the request when a new server registers (D15 SEC-23);
         - is used only by organization servers that use the built-in apps.
         The legal basis is Art. 6(1)(f), as for hosting.
       - Change "sends nothing to the project" (lines ~45-54) to name the one exception: an organization server's registration of its host name.
       - Update "Last updated".
    2. Change the sentences that say an administrator must register an app and ship a bundle, so they say PrivacyFence ships its own app and an organization may use its own:
       - website/connectors/index.html:203
       - google-workspace/index.html:155-156
       - jira-confluence/index.html:117-118
       - salesforce/index.html:92-93
       - slack/index.html:95-96: remove "The app is never publicly distributed".
       - miro/index.html:61
       - website/enterprise/index.html:85: also mention the Admin page.
       Keep each change to those sentences.
    3. If tests/unit/test_website_claims.py pins a sentence you changed, update that expected text, and nothing else.
    4. Run python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_build_site.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_website_links.py -q. If the hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_website_layout.py tests/integration/test_website_consent.py -q.
  acceptance:
  - python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_build_site.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_website_links.py -q passes
  - grep -n "oauth.privacyfence.eu" website/privacy/index.html matches
  - grep -rn "never publicly distributed" website/connectors/slack/index.html finds nothing
- id: p14-retire-plan
  title: Delete the plan document and its manual-steps page
  depends_on:
  - p13a-docs-operator
  - p13b-docs-user
  - p13c-website
  complexity: S
  touches:
  - docs/builtin-oauth-apps-plan.md
  - docs/builtin-oauth-apps-plan-manual-steps.html
  brief: |-
    The decisions were extracted into ADRs in p12.
    1. Delete docs/builtin-oauth-apps-plan.md and docs/builtin-oauth-apps-plan-manual-steps.html.
    2. Run grep -rn "builtin-oauth-apps-plan" --exclude-dir=.git . and remove any remaining reference.
    3. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q.
  acceptance:
  - test ! -e docs/builtin-oauth-apps-plan.md && test ! -e docs/builtin-oauth-apps-plan-manual-steps.html
  - grep -rn "builtin-oauth-apps-plan" --exclude-dir=.git . finds nothing
  - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q passes
```
