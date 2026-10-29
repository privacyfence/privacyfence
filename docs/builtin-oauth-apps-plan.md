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
  carry uses the **built-in PrivacyFence-owned app** baked into the server distributions (the
  org image and the PyPI wheel), as Telegram's app credentials are (ADR 0040). Built-in apps are
  for organization mode only, including single-user mode; desktop mode, deprecated in 7, gets none
  (D19).
  - **Organization servers go further (D25).** They read no bundle at all: their startup settings
    come from the environment, and an organization's own app is a signed per-provider
    **connector config** uploaded on the Admin page. An upgraded server reads its 6.x bundle,
    read-only, until 8.x. Desktop installs keep the bundle until desktop mode is removed.
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
  a server that has proven it controls its own domain. Desktop installs do not use built-in apps
  (D19).

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
  7.0 ships.
- **7.0 is tested as an alpha.** After this plan's PR merges, the maintainer cuts `7.0.0a1` and
  tests it installed the regular way: the published Docker image and the pre-release installers.
  7.0.0 follows once those checks pass (D23).

An organization that wants its own app for a provider puts the credentials in the bundle, as today.
The Admin page shows, for each service, which app is in use and how to switch.

Grist and Peek have no built-in app. Their servers and OAuth apps belong to each organization.

## Current state

- **Which connectors are offered.** `org_mode.CONNECTOR_ORG_SECTION` and
  `org_mode.connector_enabled_by_org` (`src/privacyfence/org_mode.py:660-672`, ADR 0196) offer a
  connector only when `org_config.json` has a non-empty section for it. An install with no bundle
  offers nothing. Callers:
  - `daemon_main.build_connectors` (`daemon_main.py:1677`, `:1711`, `:1726`, `:1745`)
  - `settings_controller._org_allows` (`:1297`) and `_connectors_state` (`:2180`)
  - `web/routes_connect._is_configured` (`:175-192`), `telegram_start` (`:578`) and
    `_render_connect_page` (`:1054`)
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
    and `_connectors_for_principal` (`daemon_main.py:1200-1215`).
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
- **ADR numbers.** The highest accepted ADR on main is 0253 (6.12.0's AI-proposed scoped token ADRs, 0250 to 0253), so this plan takes 0254 to 0266.
- **Operational sections and their parsers.** Each takes the whole org config dict:
  - `download_delivery`: `org_mode.DownloadDeliveryConfig.from_org_config`
  - `audit_forwarding`: `org_mode.AuditForwardingConfig.from_org_config`
  - `authz`: `org_mode.AuthzPolicyConfig.from_org_config`
  - `web_push`: `org_mode.WebPushConfig.from_org_config`
  - `step_up`: `step_up_config.StepUpConfig.from_org_config` (`step_up_config.py:287`)
  - `scoped_tokens`: `scoped_tokens.policy_from_org_config` (`scoped_tokens.py:180`)
  - `unattended_sessions.enabled`: `daemon_main.py:2152`
  - `rooms`: `daemon_main.py:1468`, written into the bundle by `scripts/sync_room_directory.py`
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

Six built-in app keys, all for organization mode and all on the relay (D19, D20):

| Key | Used for | Redirects registered on the app (manual step mb1) |
|---|---|---|
| `google_web` | Google | `https://oauth.privacyfence.eu/callback/<g>` for `g` in gmail, drive, calendar, contacts, tasks, apps_script (a Web client) |
| `slack` | Slack | `https://oauth.privacyfence.eu/callback/slack` |
| `salesforce` | Salesforce | `https://oauth.privacyfence.eu/callback/salesforce` |
| `atlassian_relay` | Atlassian | `https://oauth.privacyfence.eu/callback/atlassian` |
| `miro` | Miro | `https://oauth.privacyfence.eu/callback/miro` |
| `google_signin` | the opt-in built-in Google sign-in (D20), in its own project `privacyfence-signin` | `https://oauth.privacyfence.eu/callback/idp`, `/callback/idp-login`, `/callback/stepup` |

No app carries an `http` redirect: Slack, Miro and Atlassian refuse to distribute an app that does.
The key names keep `_web` and `_relay` so the repository secrets named in mb1 stay as they are.

Every app has two fields, `client_id` and `client_secret`.

**Generator: `scripts/builtin_apps.py`.** Stdlib only, modelled on `scripts/telegram_credentials.py`.

- `write` reads, for each key `K` above, the environment variables
  `PF_APP_<K upper>_CLIENT_ID` and `PF_APP_<K upper>_CLIENT_SECRET`, for example
  `PF_APP_GOOGLE_WEB_CLIENT_ID`. Its behaviour:
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
  "Telegram" replaced by "built-in OAuth apps" and the ADR number changed to 0254. For example, the
  error line ends `...; built-in OAuth apps will not work on this install. A stable release must
  ship it (ADR 0254).`

**Reader: `app_credentials.builtin_app(key: str) -> dict[str, str] | None`.**

- It does a literal `from . import _builtin_apps`, so that PyInstaller's static analysis bundles the
  module, exactly as `telegram_app_credentials` does. It returns a copy of `APPS.get(key)`.
- On `ImportError` it falls back to the env var `PRIVACYFENCE_BUILTIN_APPS`: the path of a JSON
  file with the same `{key: {"client_id", "client_secret"}}` shape, for development. A missing,
  unreadable or malformed file returns `None` and logs one warning without the file content.
- It also exposes `BUILTIN_APP_KEYS: tuple[str, ...]`, the six keys in the table order above.

**Accounts (mb1).** The apps belong to PrivacyFence, and no `tkcs.name` address appears anywhere a
provider shows or mails it.

- Slack, Salesforce, Atlassian and Miro are registered under `apps@privacyfence.eu`, a role alias
  on the maintainer's Workspace user `andras@tkcs.name`. The accounts are created by email, never
  with "Sign in with Google", which would make them `andras@tkcs.name` accounts. They use
  two-factor sign-in, and every support or vendor contact is `info@privacyfence.eu` (a Workspace
  group).
- The Google Cloud projects (`privacyfence-builtin`, `privacyfence-signin` and the relay's
  notification project) must belong to a Workspace user, so `andras@tkcs.name` owns them, inside
  the Workspace organization. Their user support email and developer contact are
  `info@privacyfence.eu`.
- `test@privacyfence.eu` stays the QA account and the relay's sender (`relay-notify@`, D15
  SEC-23). It owns no app.

The built-in client secrets are public in effect, just as Telegram's hash is (ADR 0040). This is
accepted: each one identifies the PrivacyFence app, and every sign-in still needs the person's own
consent.

### D2. What the bundle still holds

From 7.x on, the bundle file is `org/org_bundle.json`, adopted from `org_config.json` by copying
(D13). "Bundle" elsewhere in this plan means that file. It keeps its loading, signing, serial and
hash-log rules (`daemon_main.load_org_config`, ADR 0016, 0200).

**What 7.x reads from it depends on the mode (D25).**

| Install | What is read | How |
|---|---|---|
| Desktop (local) | the app credentials below, bookkeeping, the `mcp` check | at every start, as in 6.x; installed on the Admin page (D6) until 8.x removes desktop mode |
| Organization server upgraded from 6.x | `mode`, `server`, `idp` | read-only at every start, until `privacyfence-app --retire-bundle` (D25); never replaced |
| Organization server upgraded from 6.x | the app credentials below | imported once as connector configs (D25), then ignored |
| New organization server, or single-user | nothing | there is no bundle, and a new one is refused (D25) |

**App credentials and bookkeeping.** Field names are unchanged, and connector configs use the same
names in organization mode (D25), so every connector reads the same section dict as today.

| Key | Contents |
|---|---|
| `google` | `client_id`, `client_secret`, `auth_uri`, `token_uri` |
| `slack` | `client_id`, `client_secret`, optional `user_scopes` |
| `salesforce` | `consumer_key`, `consumer_secret`, optional `login_url` |
| `atlassian` | `client_id`, `client_secret` only |
| `miro` | `client_id`, `client_secret` |
| `grist` | the OAuth app only: `client_id`, `client_secret`, `auth_server_url`, `server_url` (the server the app belongs to, ADR 0143) |
| bookkeeping | `version`, `org_name`, `serial`, `signature`, `signing_public_key`, and whatever `org_bundle_signing` adds |
| `mcp` | still refused when it carries `tool_annotations` (ADR 0089) |

A desktop bundle has no `mode: org`, `server` or `idp`; `build_org_bundle.py` no longer writes them
(D7). A bundle that has them is an upgraded 6.x organization bundle.

**Legacy operational keys.** `admin_config.LEGACY_BUNDLE_KEYS` is this tuple:
`("download_delivery", "web_push", "authz", "step_up", "scoped_tokens", "audit_forwarding",
"unattended_sessions", "rooms", "rooms_synced_at", "telegram", "peek")`. `peek` includes main's
`peek.auto_provision` (D18). These legacy sub-keys are also treated as operational:

- `atlassian.assets`
- `atlassian.assets_workspace_id`
- `grist.server_url` when `grist` has no `client_id` (the API-key server address)
- `grist.auto_provision` (D18)

In both modes all of these are imported once into the admin config (D3) and afterwards ignored
(D4). The function `admin_config.legacy_keys_in(bundle) -> list[str]` returns the ones present, as
dotted names (for example `["authz", "atlassian.assets"]`).

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
    - In an org-mode single-user install (D9), google, slack, salesforce, atlassian, miro and
      telegram are `true`.
    - In local mode only telegram is `true`: desktop mode has no built-in apps (D19), so the other
      services need a bundle section, as in 6.x.
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
- `BUILTIN_KEY = {"google": "google_web", "slack": "slack", "salesforce": "salesforce",
  "atlassian": "atlassian_relay", "miro": "miro"}`. It is used in organization mode only (D19).

- `AppSource = Literal["bundle", "builtin", "none"]`.
- `credentials_source(bundle, service, *, mode) -> AppSource` is defined for the five services
  above:
  - `"bundle"` when the bundle section has a `client_id`, or a `consumer_key` for salesforce;
  - `"builtin"` when `mode == "org"` and `app_credentials.builtin_app(BUILTIN_KEY[service])` is not
    None;
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
- The CLI dispatch (`daemon_main.py:2658`) uses
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
  - `run_app` passes it to `_maybe_start_web_server(..., bundle=bundle)` (called at `:2172`);
  - that passes it to `_start_org_web_server(..., bundle=...)` (called at `:820-823`);
  - that stores it in a new `OrgAuth` field, `bundle: dict[str, Any]`.

  The Admin dispatcher (D6) needs it to recompute the effective config.
- The QA recorder moves to organization mode and a new runner; see D17.

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
  (`daemon_main.py:1261`), with the shared `org_config` and `daemon_main._shutdown_event`
  (`:270`). It is followed by `relay_client.request_check()` once the server reports ready. If
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
    - Rate limits, per D15 SEC-03 and SEC-25: a repeated failed attempt for the same host from the
      same `CF-Connecting-IP` within 60 s gives 429 `{"error":"too_soon"}`, and the per-IP limits
      give 429 `{"error":"too_soon"}` or `{"error":"too_many_new_hosts"}`. Attempts are counted in
      the separate `attempts` table, never as a `registrations` row; a failure never delays a
      later valid proof from another address.
    - Fetch `https://<host>/.well-known/privacyfence-relay` with `redirect: "manual"`, a 5 s
      timeout (`AbortSignal.timeout(5000)`) and the body read up to 256 bytes. A non-200 response,
      or a trimmed body different from `token`, gives 403 `{"error":"proof_failed"}`.
    - On success, upsert the row: `registered_at` (kept from the first registration),
      `verified_at = now` and `last_attempt_at = now`. Only a successful proof creates a row. Return
      200 `{"host", "verified_at"}`.
  - `GET /callback/:grant`, where `grant` is in `gmail`, `drive`, `calendar`, `contacts`, `tasks`,
    `apps_script`, `slack`, `salesforce`, `atlassian` or `miro`:
    - The query must carry exactly one `state`, and exactly one `code` or exactly one `error`
      (D15 SEC-10, SEC-24). Otherwise 400.
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
  7.0.0a1 (D23). The feature PR merging to `main` deploys it, and `/cut-release` and `docs/releasing.md`
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
    - a loopback `/callback` with no `code` and no `error` returns 400 (SEC-24).
  - **Revoking a host.** The curl call.
  - **Rotating `REVOKE_TOKEN`.** Update the environment secret, then run the workflow on `main`.
  - **Rolling back.** `npx wrangler rollback`, or the dashboard's Deployments tab.
  - **Reading the registrations.**
  - **The compatibility rule** above.
- **Cloudflare resources.** `docs/downloads-and-release-kpi.md`'s "Cloudflare resources" table
  gains rows for the Worker `privacyfence-oauth-relay`, the custom domain `oauth.privacyfence.eu`
  and the Durable Object class `Registry`. Its credentials table gains rows for the three
  `CF_OAUTH_RELAY_*` secrets.

**Accepted risks**, recorded in ADR 0258:

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
    `routes_connect.py:1085`, `routes_approvals.py:505`, `routes_whats_new.py:93` and
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
| `set_salesforce_login_url` | `value: str` (D26) | both | yes | live |
| `set_server_address` | `service: str` (`grist` or `peek`), `value: str` | both | yes | live (refused while the address comes from the environment, D18) |
| `set_auto_provisioning` | `service: str` (`grist` or `peek`), `enabled: bool, trust_unverified_email: bool` | org (not single-user) | yes | restart (D18) |
| `set_unattended_sessions` | `enabled: bool` | both | yes | restart |
| `clear_rooms` | — | both | no | live |
| `set_download_delivery` | `inline_max_bytes: int, link_ttl_seconds: int, allow_disk_staging: bool, agent_links: bool, allow_exports: bool` | org | yes | restart |
| `set_web_push` | `enabled: bool` | org | yes | restart |
| `set_authz` | `allowed_domains: str, groups_claim: str, required_groups: str` (comma- or newline-separated lists) | org | yes | restart |
| `tighten_step_up` | `enabled: bool, require_passkey: bool, scope: str` | org | yes | restart |
| `set_scoped_tokens` | `enabled: bool, max_days: int, allow_ai_proposals: bool` (D21) | org | yes | restart |
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
- ADR 0256 records this.

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
    `_connectors_for_new_principal` closes over (`daemon_main.py:876-878`), so other OS accounts'
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

**`scripts/build_org_bundle.py` builds desktop bundles only:** app credentials (D2) and
bookkeeping, signed as today. The script's own copy of the canonicalization and signing logic is
unchanged (ADR 0016). It stays a release asset next to `sync_room_directory.py`, because desktop IT
still runs it; `sign_connector_config.py` and the browser tool join them (D25).

Every refused option is hidden with `help=argparse.SUPPRESS` and stays parseable only to be
refused. The script prints one `error:` line for each refused option given, then exits 2 without
writing anything.

- **Organization-mode options (D25).** `--mode org`, and every option whose destination starts with
  `server_` or `idp_` (`--server-issuer-url`, `--server-bind-host`, `--server-port`,
  `--server-trusted-proxy`, `--idp-issuer`, `--idp-client-id`, `--idp-client-secret`,
  `--idp-admin-group-claim`, `--idp-admin-group-value`, `--idp-step-up-acr-value`, and any other
  `--idp-*`). The message is `error: <option>: organization servers take their settings from
  privacyfence.env and their own apps from signed connector configs (docs/org-mode-setup-guide.md,
  ADR 0268).` `--mode local` remains, and is the default.
- **Operational options (D3).** They write a legacy key. The message is `error: <option> has moved
  to the Admin page (ADR 0256); remove it.`
  - `--enable-telegram`, `--enable-peek`, `--enable-grist` and `--peek-base-url`;
  - `--grist-server-url` when given without `--grist-client-id`;
  - `--atlassian-assets` and `--atlassian-assets-workspace-id`;
  - `--authz-allowed-domain`, `--authz-groups-claim` and `--authz-required-group`;
  - `--step-up-enabled`, `--step-up-disabled`, `--step-up-scope`, `--step-up-rp-id`,
    `--step-up-rp-name`, `--step-up-require-passkey` and `--step-up-no-require-passkey`;
  - `--enable-unattended-sessions` and `--disable-unattended-sessions`;
  - `--enable-scoped-tokens`, `--disable-scoped-tokens`, `--scoped-token-max-days`,
    `--enable-scoped-token-proposals` and `--disable-scoped-token-proposals` (D21);
  - `--downloads-inline-max-bytes`, `--downloads-link-ttl-seconds`, `--downloads-disable-staging`,
    `--agent-links`/`--no-agent-links` and `--exports`/`--no-exports`;
  - `--web-push`/`--no-web-push` (one `BooleanOptionalAction`);
  - `--enable-audit-forwarding`, `--disable-audit-forwarding` and every
    `--audit-forwarding-*` option;
  - main's auto-provisioning options (D18): `--peek-auto-provision`, `--no-peek-auto-provision`,
    `--peek-provisioning-key-env`, `--peek-trust-unverified-email`, `--grist-auto-provision`,
    `--no-grist-auto-provision`, `--grist-provisioner-url`, `--grist-provisioning-key-env` and
    `--grist-trust-unverified-email`. Their message also names the environment variables:
    `error: <option> has moved to the Admin page and PRIVACYFENCE_PEEK_BASE_URL /
    PRIVACYFENCE_GRIST_SERVER_URL / PRIVACYFENCE_GRIST_PROVISIONER_URL on the server (ADR 0256,
    0263); remove it.`
- **`--merge`.**
  - It refuses an input that carries `mode: org`, `server` or `idp`: `error: <file> is an
    organization bundle; organization servers no longer take bundles (ADR 0268).`
  - It drops legacy operational keys from the merged bundle and prints `note: dropped <names>;
    they are managed on the Admin page now.`
- **Also update:**
  - the "nothing to write" check (`build_org_bundle.py:1012-1020`), which names unattended
    sessions and scoped tokens, so it names only app credentials;
  - `--mode local`'s popping of operational keys (`:819-829`), which now has nothing to pop;
  - the module docstring and `--help` epilog: a desktop bundle builder, pointing organization
    servers to `privacyfence.env` and connector configs;
  - `tests/unit/test_docs_configuration_reference.py:128-135`: `_bundle_options()` skips actions
    whose `help is argparse.SUPPRESS`, so the reference no longer has to list refused options;
  - `tests/unit/test_org_mode.py:98-99`'s comment and the test it names in
    `test_build_org_bundle.py`, which pin the builder's default bind host to `ServerConfig`'s.
    The builder no longer writes a `server` section, so that pin moves to
    `deploy/docker/privacyfence.env.example`'s commented default instead.

**`scripts/sync_room_directory.py`.** It now writes a standalone file: `--output rooms.json`
replaces `--org-config`, and so do the bundle-merge, signature and `--sign-key` logic. The file is
`{"rooms": [...], "rooms_synced_at": "<iso>"}`, mode 0600. The administrator uploads it on the
Admin page. Passing `--org-config` prints `error: --org-config has been replaced by --output;
upload the file on the Admin page (ADR 0256).` and exits 2.

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
  not None (`web/server.py:1349`, `:1373`), and the issuer log line (`daemon_main.py:1265-1267`)
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
  will be removed in PrivacyFence 8. PrivacyFence's built-in sign-in for Google, Slack, Salesforce,
  Atlassian and Miro works only in organization mode and the Docker image. Move to an organization
  server, or try the Docker image on this computer:
  https://privacyfence.eu/docs/moving-to-organization-mode/`
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
- **Release.** The maintainer cuts 7.0.0a1 after the merge, and 7.0.0 once the manual checks pass
  on the alpha (D23), per `docs/releasing.md`. No phase tags anything.

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
    `daemon_main.py:2623`), before anything reads or rewrites data, including
    `load_config(args.config, principal=LOCAL_PRINCIPAL)` at `:2650`, the CLI OAuth flags and
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

**What 8.x does with it.** This is recorded in ADR 0261 and is not built here.

- At startup, if the data directory holds existing data (`settings.yaml`, `credentials/`, `users/`
  or `org/`) and the marker is missing or has `format < 7`, 8.x refuses to start with: `This data
  comes from a PrivacyFence release older than 7. Install PrivacyFence 7.x, start it once so it can
  upgrade your data, then install this release.`
- A data directory with no existing data is a fresh install, and 8.x writes `format` 8.
- 8.x's installers (deb `preinst`, the macOS pkg `preinstall`, the Windows installer) may read the
  same file before replacing anything, so an upgrade stops before the old release is removed.
  Because of that, the marker's path and its `format` key are a stable contract.

### D13. The 6 → 7 upgrade is reversible

A 7.x install must be able to go back to the latest 6.x release (6.10.0 or newer) and keep running with the
`org_config.json` it had. So:

- 7.x never writes, renames or deletes a 6.x bundle or pin file.
- Every other file 6.x also reads stays in a format 6.10.x reads. That covers settings.yaml,
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
  - `scripts/build_org_bundle.py`'s `--output` default becomes `org_bundle.json` (`:247`);
  - `scripts/qa_authenticate_connectors.py:61`: the default path constant changes to the new
    name. The backup name at `:119` uses `DEFAULT_ORG_CONFIG_PATH.name` instead of a hard-coded
    `org_config.json`, and the docstring (`:24`, `:41`, `:107`) names the new file;
  - user-facing messages that name `org_config.json` as the file to edit. Those are in
    `daemon_main.load_org_config`, about 35 in `org_mode.py` (`:51-654`, including the Peek and Grist auto-provisioning messages), `step_up_config.py:293`
    and `:299`, and the D9/D10 messages in this plan. They say `org_bundle.json`. p6's mapping of
    parser errors strips `"org_bundle.json's "` accordingly. `web/mcp_dispatch.py:653` and
    `sync/src/privacyfence_sync/runner.py:41` are rewritten by p18.
- **The other 7.x writes are 6.x-compatible.**
  - `admin_config.json`, `data_format.json`, `relay_token.txt` and `org_bundle_adopted.json` are
    new files that 6.x ignores.
  - The settings.yaml and scoped_tokens.json rewrites D12 performs produce formats the latest 6.x
    already reads: version-3 rows, canonical verb names, tokens holding their own rules. 6.10.0 is the first release with all three: it
    introduced tokens holding their own rules, and 6.9.x cannot read them.
  - Peek and Grist accounts connected by auto-provisioning (added in 6.11.0) keep working after
    a downgrade to 6.11.0 or newer, which reads the same bundle. The docs name the latest 6.x, at
    least 6.11.0, as the release to go back to on a server that uses them.
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
- **8.x (ADR 0261's list).** 8.x deletes `adopt_legacy_files` and the legacy constants along with
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
  ADR 0263 records this as an amendment to 0016.
- An upgraded server's legacy bundle, whose `idp` wins over the environment, is the only signed
  bundle an organization server reads (D25, D15 SEC-14).

**A server stays a server.** The first start for which `_idp_present` is true sets
`"org_server": true` in the admin config. It does this before IdP discovery, so a server whose IdP
is down still records it. `admin_config.apply_change` never changes this key, and the Admin page
does not show it as editable. D9's single-user refusal also fires when that flag is
set, so removing the IdP variables never turns a real server into single-user mode.

**Admins by email.** `IdpConfig` gains `admin_emails: tuple[str, ...]`, ASCII lower-cased, from the
section's `admin_emails` list.

- `org_identity.principal_from_claims` sets `is_admin` when the group rule matches, **or** when
  `isinstance(claims.get("email"), str)`, its ASCII `.strip().lower()` is in `admin_emails`,
  **and** `claims.get("email_verified") is True`. The configured list is normalized the same way.
  An address with a non-ASCII character never matches, on either side (D15 SEC-07: not
  `casefold`, which maps `ſ` to `s`).
- A multi-tenant issuer such as `accounts.google.com` accepts any Google account that has verified
  the address. The docs recommend pairing the email list with allowed domains (Admin → Sign-in), or
  an Internal consent screen.
- An IdP that does not send `email_verified` (Microsoft Entra ID, for example) cannot name admins
  by email. The docs tell those admins to use the group claim.
- Admins by email come from `PRIVACYFENCE_ADMIN_EMAILS` only. `build_org_bundle.py` writes no
  `idp` section any more (D7, D25).

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
- ADRs 0254 and 0258 record this threat precisely.

**SEC-23: every new registration emails the maintainers.** (p11, p13c, manual mb2, ma0) There
is no waiting period: a new host is served at once, and the maintainers hear about it right away.

- **Sending: the Gmail API, as a send-only alias of the QA Workspace account.** `privacyfence.eu`
  mail is on Google Workspace, so Cloudflare Email Routing cannot be enabled on it without
  breaking that mail. The Worker therefore sends through Gmail.
  - **The sender.** `relay-notify@privacyfence.eu`, an alias of `test@privacyfence.eu`, set up as
    a Gmail "Send mail as" address. `NOTIFY_FROM` (a `[vars]` entry in `wrangler.toml`) holds it.
    The recipient is the constant `NOTIFY_TO = "info@privacyfence.eu"` in `src/notify.ts`, never
    taken from a request.
  - **The credential.** An OAuth client of type Web application, with audience **Internal**, in a
    Google Cloud project of the `privacyfence.eu` Workspace. An Internal app needs no verification
    and its refresh tokens do not expire after seven days. Its refresh token is minted for
    `test@privacyfence.eu` with the single scope `https://www.googleapis.com/auth/gmail.send`, so it
    can send but never read mail. Three Worker secrets hold it: `GMAIL_CLIENT_ID`,
    `GMAIL_CLIENT_SECRET` and `GMAIL_REFRESH_TOKEN`.
  - **The deploy.** The deploy workflow sets the three secrets with `wrangler secret put`, from the
    `oauth-relay-worker` environment secrets `CF_OAUTH_RELAY_GMAIL_CLIENT_ID`, `..._CLIENT_SECRET`
    and `..._REFRESH_TOKEN`, as it does `REVOKE_TOKEN`.
  - **The calls (`src/notify.ts`).**
    - It exchanges the refresh token at `https://oauth2.googleapis.com/token`
      (`grant_type=refresh_token`), and keeps the access token in memory until 60 seconds before
      its `expires_in`.
    - It then POSTs `{"raw": <base64url RFC 5322 message>}` to
      `https://gmail.googleapis.com/gmail/v1/users/me/messages/send`.
    - Both calls use `fetch` with a 10-second `AbortSignal.timeout`. There is no SDK and no new
      dependency.
  - **Missing configuration.** When any of the three secrets or `NOTIFY_FROM` is empty, the Worker
    sends nothing and counts `notify_skipped`.
  - **Failures.**
    - Each failure increments `notify_failures` and records `notify_last_error`. That is a short
      code, never a secret or a response body: `token_invalid_grant`, `token_http_<status>`,
      `send_http_<status>` or `network`.
    - `token_invalid_grant` means the refresh token was revoked, for example after a password
      change on `test@`. The runbook explains how to mint a new one.
  - **Containing a stolen token.** Whoever holds the token can send mail as
    `relay-notify@privacyfence.eu`, or as `test@privacyfence.eu` itself, since the sender is an
    alias. So `test@privacyfence.eu` sits in its own organizational unit with a Gmail **content
    compliance** rule for **outbound** mail only (Admin console → Apps → Gmail → Compliance): a
    message whose envelope recipient is not in `privacyfence.eu` or `tkcs.name` is rejected. A
    leaked token cannot phish anyone outside.
    - Not **Restrict delivery**: it also blocks incoming mail, and the QA account must keep
      receiving the providers' sign-up and verification emails (it blocked Salesforce's).
    - The QA account never sends mail through PrivacyFence; its tests only draft.
    - The deploy path is guarded by the same required reviewers as the relay itself (SEC-05).
    - ADR 0258 and the runbook record this residual risk and the rotation steps: revoke the token
      at myaccount.google.com/permissions as `test@`, mint a new one, update the environment
      secret, redeploy.
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
  - Headers are `From`, `To`, `Subject`, `Date`, `Message-ID` (`<uuid@<NOTIFY_FROM's domain>>`),
    `MIME-Version`, and `Content-Type: text/plain; charset=utf-8`.
  - The host is already validated as ASCII by the host rules, so no header injection is possible.
    The code still refuses to send if any header value contains `\r` or `\n`.
- **Never blocks a registration.** Sending runs in `ctx.waitUntil`. A failure is caught and
  counted in the Durable Object (`notify_failures`, `last_notify_failure_at`). Both are shown in
  the maintainer-only `/registrations` response, so failures are visible although the Worker
  keeps no logs.
- **Tests** (stub `globalThis.fetch` with `vi.spyOn` for the token and send endpoints):
  - a first registration sends exactly one message: decoding `raw` gives `From:
    relay-notify@privacyfence.eu`, `To: info@privacyfence.eu`, the host, the IP and the revoke
    command;
  - the access token is reused within its lifetime and refreshed after it;
  - a re-verification sends none;
  - the 51st first registration of a day sends the one over-the-limit message, and the 52nd
    sends nothing;
  - an `invalid_grant` token response, a 500 from either endpoint, a timeout and a thrown `fetch`
    each still return 200 to `/register`, increment `notify_failures` and set the matching
    `notify_last_error`, and no secret appears in `/registrations` or any response;
  - a missing secret counts `notify_skipped` and makes no outbound call;
  - a header value with a newline is refused.
- **The weekly review stays** (SEC-02). The email is the prompt to act on a suspicious host the
  same day.
- **Privacy.** The privacy page (p13c) says the relay emails the maintainers the registering
  server's host name, the request's IP address and country when a new server registers, through
  PrivacyFence's Google Workspace mail.

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
- The relay stays in the existing Cloudflare account, by maintainer decision. ADR 0258 and
  `docs/oauth-relay.md` record that a Workers deploy token covers every Worker in the account, so
  the relay token could replace the download Worker and the reverse. Required reviewers are the
  control.
- Slack and Miro send no PKCE. A compromised relay could exchange their codes first, or inject an
  attacker's own account. ADR 0258 and `security-and-compliance.md` state this per provider.
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
- **Entra ID and other providers without `email_verified`:** `idp.trust_unverified_email` lifts
  the allowed-domains requirement only (D18).

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

- **Desktop installers ship no built-in app credentials (D19).** `build_dmg.sh`, `build_deb.sh` and
  `build_installer.ps1` do not call `scripts/builtin_apps.py write`. Only the org image's wheel and
  the PyPI build include the module, because servers use them.
- **Rotation.** ADR 0254 and `docs/oauth-relay.md` (a "Rotating a built-in app secret" section)
  describe rotation: add the new secret alongside the old where the provider allows two (Google
  and Salesforce do), release, then retire the old one after the release is adopted.

**SEC-14: a signed bundle locks the IdP; no wildcard proxies.** (p20a, p15)

- **The IdP.** On an organization server, a legacy bundle's `idp` section wins over
  `PRIVACYFENCE_IDP_*` and `PRIVACYFENCE_ADMIN_EMAILS`, which then log D14's warning. A legacy
  organization bundle without an `idp` section (6.x never wrote one) refuses the start with
  `ConfigurationError("org_bundle.json has no \"idp\" section; set PRIVACYFENCE_IDP_* and run
  privacyfence-app --retire-bundle.")`. No bundle option exists to delegate the IdP to the
  environment, because organization bundles can no longer be built (D7, D25).
- **Trusted proxies.** `trusted_proxies` from any source refuses `*`, `0.0.0.0/0` and `::/0` with
  `trusted_proxies must name specific proxies, not a wildcard.`

**SEC-15: "no logs" means the Worker writes none.** (p11, p12, p13c)

- The privacy page and ADR 0258 say "the Worker writes no logs".
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
- ADR 0256 says room text reaches agents as untrusted data.

**SEC-21: the marker is advisory; backups are private.** (p19a, p12)

- `<file>.pre-canonical-verbs.bak` and `.pre-scope-rules.bak` are written with mode 0600 through
  `secure_files`.
- ADR 0261 requires 8.x to keep reading pre-7 shapes fail-closed rather than trusting the marker,
  and its installers not to follow symlinks when reading it.

**SEC-22: public secrets and refresh tokens.** (p12)

ADR 0254 notes that with built-in apps a stolen refresh token no longer also needs the org's own
client secret.

The rules SEC-24 to SEC-26 come from a later, fresh security review of the whole plan.

**SEC-24: a relay callback forwards only a code or an error.** (p11)

- `GET /callback/:grant` requires exactly one `state`, and exactly one `code` or exactly one
  `error`, in the query. Anything else, including a request with neither, a request with both, or
  any repeated parameter (SEC-10), gets 400 and no redirect.
- **Why.** A browser keeps a URL fragment across a 302 whose `Location` has none. A crafted sign-in
  that asks a provider for a token in the fragment (an implicit or hybrid response type, with the
  relay's registered redirect URI) must never be carried on to a host. Every flow PrivacyFence
  starts is the code flow, so nothing legitimate is refused.
- **Tests:** a callback with only `state`; with `state` and `access_token`; with `code` and
  `error` together; and with no query at all each get 400 with no `Location`.

**SEC-25: the registration emails cannot be drowned out cheaply.** (p11, p5)

- One server with wildcard DNS can prove any number of host names with one token. Fifty throwaway
  names would use up SEC-23's daily email cap, and a phishing host registered after them would get
  no email of its own.
- **Per-address cap.** The Worker accepts at most 5 *new* hosts (a host's first successful proof)
  per `CF-Connecting-IP` per UTC day, counted in the Durable Object. The next one gets 429
  `{"error":"too_many_new_hosts"}` before any proof fetch, and no row is created.
  Re-verifications of known hosts do not count.
- **Nothing goes unseen.** A registration that sent no email (over the cap, or a send failure)
  is listed by `/registrations` with `"notified": false`, and the weekly review (SEC-02) starts
  with those rows.
- **The server.** `relay_client` treats `too_many_new_hosts` like `too_soon`: retry later with the
  backoff, keep `last_ok_at`.
- **Tests:** the sixth new host from one address in a day is refused and creates no row; a
  re-verification does not count; another address is unaffected; an unnotified row shows
  `"notified": false`.

**SEC-26: the relay token is kept, not silently replaced.** (p5, p20e, p13a)

- `org/relay_token.txt` is the proof behind every registration and, with the built-in Google
  sign-in (D20), the value of every `_privacyfence-relay` DNS TXT record (`pf-relay=<sha256>`).
- `well_known_token()` still creates a missing or malformed file. When `org/relay_status.json`
  records an earlier success, it also logs at ERROR, once per start: `org/relay_token.txt was
  missing or unreadable and has been replaced. The server registers again with the new token. With
  the built-in Google sign-in, every _privacyfence-relay DNS TXT record must be updated; the lines
  below give the new values.`
- The docs (p13a) list `relay_token.txt` and `relay_status.json`, with `admin_config.json`, as files
  to keep in the data-volume backup.
- **Test:** a replaced token after a recorded success logs the ERROR line once and never the
  token.

### D16. Relay terms and the website

Hand-written website pages deploy from `main` on merge (pages.yml); `/docs/` follows the newest
stable tag (ADRs 0052, 0099). The relay also goes live on merge. So the website work splits in two.

**In this PR (p13c), live together with the relay:**

1. **Relay terms, `website/relay-terms/index.html` (new).** Kicker "Legal", title `Relay terms —
   PrivacyFence`, canonical `https://privacyfence.eu/relay-terms/`, a "Last updated" line, built
   from `website/imprint/index.html`'s structure. Sections and their substance:
   - *What the relay is.* A free, non-commercial service at `oauth.privacyfence.eu` that forwards
     an OAuth sign-in redirect for PrivacyFence's built-in apps to the PrivacyFence server that
     started it. Operated by the publisher named in the imprint (link `/imprint/`).
   - *As is, no warranty.* Provided "as is" and "as available", without warranty of any kind,
     express or implied, including availability, fitness for a purpose and non-infringement. No
     service-level agreement, no support obligation.
   - *Limitation of liability.* To the extent permitted by applicable law, the publisher is not
     liable for any damage arising from using or being unable to use the relay, including lost
     data, lost access, interrupted sign-ins or costs of replacement apps. This does not limit
     liability that cannot be limited by law, such as for intent or gross negligence, or for
     injury to life, body or health.
   - *Change, suspension, revocation.* The relay, the built-in apps and these terms may change,
     be suspended or end at any time, without notice. A registered host can be revoked at any
     time, in particular for abuse, a security concern or a provider's request.
   - *Your responsibilities.* The operator of a PrivacyFence server is responsible for that server,
     for the accounts people connect, and for its own legal compliance, including any records of
     processing. Each provider's own terms apply to its sign-in (Google, Slack, Salesforce,
     Atlassian, Miro). Do not register a host you do not control; do not use the relay to phish or
     to get around a provider's policy.
   - *Use your own apps.* An organization that needs guarantees registers its own apps on the
     Admin page; the relay is then not used (link `/docs/oauth-relay/`, which exists only after the
     7.0 release, so link `/docs/` until the follow-up in ma5).
   - *Privacy.* Link `/privacy/#relay`.
   - *Contact.* `info@privacyfence.eu`.

   The page carries an HTML comment and the plan's ma5 step says: have the terms reviewed by a
   lawyer before relying on them. The plan does not claim they are legally sufficient.
2. **Wiring, like `/imprint/`.** `scripts/build_site.py` (the page map at `:125`, the llms list at
   `:1144`: `- [Relay terms](.../relay-terms/): the terms for the sign-in relay at
   oauth.privacyfence.eu`), the footer partial (`website/_partials/footer.html`, after Imprint:
   `Relay terms`), and `tests/unit/test_website_pages.py`'s footer list.
3. **Privacy policy, `website/privacy/index.html`.**
   - The scope sentence (`:26-27`) names `oauth.privacyfence.eu` too.
   - A new section `<h2 id="relay">Sign-in relay (oauth.privacyfence.eu)</h2>`, after the download
     section. The relay:
     - forwards the sign-in redirect, which carries a one-time authorization code and the server's
       host name, and processes the IP address in transit;
     - stores only each registered server's host name and registration dates, visible only to the
       PrivacyFence maintainers (SEC-09);
     - writes no logs (SEC-15);
     - emails the maintainers the host name, the IP address and the country of the request when a
       new server registers (SEC-23), through PrivacyFence's Google Workspace mail (Google is the
       processor), kept in that mailbox for up to 12 months;
     - is used only by organization servers and single-user installs that use the built-in apps;
       an organization's own apps never touch it.
     Legal basis Art. 6(1)(f) (operating and securing the service). Cloudflare is the processor,
     as for the download service.
   - A new section `<h2 id="google-user-data">Google user data in the PrivacyFence app</h2>`,
     required for Google's verification of the built-in app (ma8):
     - **What it accesses.** Gmail, Drive, Calendar, Contacts, Tasks and Apps Script, only after
       the person signs in and consents. Each scope is used for what the connector pages describe.
     - **Where it goes.**
       - Tokens and data stay on the person's computer or their organization's server, never on
         PrivacyFence's.
       - Content reaches the AI assistant the person connected only when they approve it on a
         card, or when a rule they set allows it, after the local personal-data check.
       - The sign-in relay sees a one-time sign-in code, never mail or file content.
     - **Never.** No ads, no sale, no human reading, and no use to train AI models by
       PrivacyFence.
     - **The AI assistant's own terms.** They govern what it does with content it is given.
     - **Revoking.** Disconnect on `/connect`, or remove access at
       https://myaccount.google.com/permissions.
     - **Limited Use sentence, verbatim:** `PrivacyFence's use and transfer to any other app of
       information received from Google APIs will adhere to the Google API Services User Data
       Policy, including the Limited Use requirements.` It links
       `https://developers.google.com/terms/api-services-user-data-policy`.
   - "It sends nothing to the project" (`:45-54`) names the one exception: an organization server
     or single-user install that uses the built-in apps registers its host name with the relay
     and sends its sign-in redirects through it.
   - "Last updated".
4. **"No PrivacyFence-operated server" claims.** These become wrong when the relay is live, so they
   change in this PR, each to a sentence that keeps the claim about data and names the relay:
   `website/index.html:116` and `:177`, `website/enterprise/index.html:30`,
   `website/how-it-works/index.html:43`, `website/security/index.html:41` and `:100`,
   `website/faq/index.html:86`, `website/compare/claude-connectors/index.html:72`. The substance:
   *no data passes through a PrivacyFence-operated server; when an organization server uses
   PrivacyFence's built-in sign-in apps, only the sign-in redirect passes through the PrivacyFence
   relay, and an organization can use its own apps instead.* Where a page says installed copies
   keep running without the maintainer, add: new sign-ins with the built-in apps on a server
   depend on the relay; existing connections and your own apps do not.
5. **Desktop mode deprecation.** A notice on `website/download/index.html` (above "Before you
   install") and in the FAQ's `#modes` answer: `PrivacyFence 7 deprecates desktop (local) mode,
   and PrivacyFence 8 removes it. Desktop installs keep working through 7.x. Plan new deployments
   on organization mode.` linking `/enterprise/`. It says "PrivacyFence 7" rather than "this
   release", so it is true before the tag too. The download page keeps its installers.
6. **Tests.** Update an expected sentence in `tests/unit/test_website_claims.py` only where it
   pins a sentence changed above. Add to `tests/unit/test_website_pages.py`: `/relay-terms/` is in
   the page map and its canonical URL is right; the footer links it; the privacy page has
   `id="relay"`; no page outside `website/_docs/` contains `there are none` or `no
   PrivacyFence-operated server is involved`.

**The relay links its terms (p11).**

- `GET /` and `GET /terms` (and HEAD) answer 302 to `https://privacyfence.eu/relay-terms/`.
- The 400 texts end with ` Terms: https://privacyfence.eu/relay-terms/`.
- A successful `/register` response adds `"terms": "https://privacyfence.eu/relay-terms/"`.
- The relay and the pages both deploy on merge (D23), so the terms link can 404 for the few minutes
  until the merge. That window is the maintainer's own test.

**The product can see the terms (p8a, p13a).** The Admin page's relay status line ends with
`Relay terms` linking `https://privacyfence.eu/relay-terms/`. `docs/oauth-relay.md` links the
terms and the privacy section.

**After the 7.0.0 tag (manual ma5): a follow-up website PR.** Product pages that describe 7.x
features land only after the stable release that documents them (ADR 0099). The guide carries a
ready-to-run session prompt for it, covering:

- the home, download, enterprise, how-it-works, faq, security, gdpr, compare and ai-agents pages:
  built-in apps, the Admin page, the environment-only Docker start, single-user mode, and the
  desktop-mode timeline;
- the connector pages (`website/connectors/*`): PrivacyFence ships its own app, an organization may
  use its own; Slack loses "The app is never publicly distributed";
- the download page: the Docker single-user install as the recommended path for one person;
- links from the terms and privacy pages to `/docs/oauth-relay/` once it exists.

### D17. A new live-check runner: runner.privacyfence.eu

**Why.** The live checks (`connector-live-check.yml`, `qa-record-fixture.yml`) run on the
self-hosted runner labelled `privacyfence-test` in the runner group of the same name (ADRs 0019,
0181). Every token on it was minted with the QA bundle's own OAuth apps
(`scripts/qa_authenticate_connectors.py`, `docs/connector-qa.md:42-49`), and the recorder reads the
raw bundle (`daemon_main.load_org_config()`, for example `scripts/qa_fixture_recorder.py:863-870`,
`:1526-1534`). The current host also runs unrelated test services, and the built-in apps need a
public QA organization server to be tested at all. A new host replaces the old one rather than
reworking it in place.

**The host.**

- A Hetzner Cloud server: Ubuntu 24.04 LTS, x86-64, at least 2 vCPU, 4 GB RAM and 40 GB disk.
  It is dedicated to PrivacyFence: no other repository's runner and no other service.
- Host name `runner.privacyfence.eu`. It is a proxied (orange-cloud) DNS record in the
  `privacyfence.eu` Cloudflare zone, with SSL mode Full (strict) and a Cloudflare Origin CA
  certificate on the origin.
- **Firewall.** A Hetzner Cloud Firewall allows inbound TCP 443 only from Cloudflare's published
  ranges (`https://www.cloudflare.com/ips-v4` and `-v6`), and nothing else: no 22, no 80. Outbound
  is open.
- **Administration over Tailscale.** The Cloudflare proxy does not carry SSH, so the host is
  administered only over Tailscale.
  - cloud-init joins the tailnet at first boot with a one-off, pre-approved auth key and runs
    `tailscale up --ssh`.
  - The tailnet ACL lets only the maintainer reach it.
  - OpenSSH is disabled (`systemctl disable --now ssh`). Administration goes over Tailscale SSH;
    no token is ever moved by hand (D22).
- **Users.**
  - `pfrunner` (uid and gid 10001, D22) runs the GitHub runner service. It holds the QA
    configuration in `~/privacyfence` (mode 700). Its only sudo right is the no-argument deploy
    script of D22.
  - `pfrunner` is not in the `docker` group, which is root-equivalent. The live checks need no
    Docker.
  - Peek (`ghcr.io/privacyfence/peek:main`, as `docs/connector-qa.md:39` names it) and the QA org
    server run as containers of one compose project (D22). They share exactly one directory with
    `pfrunner`: the QA server's data directory.
- Unattended upgrades are on, and the runner binary updates itself (GitHub's default).

**The runner.**

- It is registered as `runner-privacyfence-eu` with the labels `self-hosted,privacyfence-live`, in
  a new organization runner group `privacyfence-live`.
- The group is set to "Selected workflows": `connector-live-check.yml`, `qa-record-fixture.yml`
  and `deploy-qa-server.yml` (D22) on `main`, as ADR 0181 requires. GitHub accepts only a
  workflow that exists on `main`, so mb4 lists the first two and ma6 adds `deploy-qa-server.yml`
  once the feature PR has merged.
- It runs as a non-ephemeral systemd service (ADR 0019 Decision 4).
- The workflows on `main` switch to `runs-on: [self-hosted, privacyfence-live]` when this plan
  merges. That is the cutover: until then the old runner keeps serving `main`'s current
  workflows, and the new one sits idle. Afterwards the old runner, its group and its QA state
  are removed.

**What the live checks test: organization mode and the built-in apps.** 8.x removes local mode
(D11), so the runner is built on organization mode now and needs no change then. It also tests the
apps most people use.

- **One QA principal.** The recorder runs as the fixed principal `qa-live`. It enters
  `principal.principal_scope(Principal(id="qa-live", ...))` once, in a new helper
  `_qa_context()`. Every token path then resolves through `daemon_main._resolve_path` to
  `<checkout>/users/qa-live/credentials/`, in the same file formats as today.
- **Organization-mode configuration.** Each `daemon_main.load_org_config()` call in the recorder
  (`:864`, `:945`, `:1283`, `:1323`, `:1527`, `:1790`, `:1880`, `:1937`, `:1984`, `:2042`,
  `:2093`, `:2171`) is replaced by `_qa_org_config()`, which returns
  `oauth_apps.effective_org_config({"mode": "org", **bundle}, QA_ADMIN, mode="org")`.
  - `bundle` is `load_org_config()`, usually empty.
  - `QA_ADMIN` offers every service in `admin_config.SERVICES`.
  - The Google, Slack, Salesforce, Atlassian and Miro sections are therefore the built-in apps,
    unless the runner's optional bundle carries an organization's own app to test.
  - Peek's and Grist's addresses come from
    `PRIVACYFENCE_PEEK_BASE_URL=https://peek-qa.privacyfence.eu` and
    `PRIVACYFENCE_GRIST_SERVER_URL=https://docs.getgrist.com` in the workflow env (D18, D22).
- **Built-in credentials on the runner.**
  - Both workflows gain a step "Write built-in app credentials" before the recorder. It runs
    `python3 scripts/builtin_apps.py write` with the ten connector `PF_APP_*` secrets (the five
    connector apps; not `PF_APP_GOOGLE_SIGNIN_*`, which the live checks do not use) in that step's
    `env:` only.
  - A last step, `if: always()`, deletes `src/privacyfence/_builtin_apps.py`, because the runner
    is not ephemeral.
  - Only the client ID and secret are used: refreshing a token needs no redirect.
- **Connecting the QA accounts (ma6), in a browser.** Every token, OAuth or not, is connected on
  the QA org server's `/connect` page, and the runner copies it from there (D22). Nobody copies an
  authorization URL, opens a tunnel or places a token file by hand.
  `scripts/qa_authenticate_connectors.py` and its loopback CLI flows are no longer used by the
  runner; 8.x can remove them with local mode.
- **The copy steps** (D22) copy the QA principal's `credentials/` from the QA server's data
  directory into the checkout's `users/qa-live/credentials/` before the recorder, and write
  changed files back after it (`if: always()`). They also copy
  `~/privacyfence/tests/fixtures/qa_environment.yaml` and an optional `org/org_bundle.json`.
  Neither names `credentials/` at the checkout root or `org/org_config.json` any more.
- `connector-live-check.yml` adds a step `curl -fsSI https://oauth.privacyfence.eu/health`
  (continue-on-error, reported in the summary as `relay: up` or `relay: down`). That gives a
  weekly external check of the relay.
- **What is not live-tested any more.** Local-mode loopback sign-in, and own apps, unless the
  optional bundle carries one. Both stay covered by unit tests, and desktop mode by ma1.

**The QA organization server.** `https://runner.privacyfence.eu` serves a PrivacyFence
organization server from `deploy/docker/`. Its only users are the maintainer and the QA Google
Workspace accounts, through a QA Google OIDC app.

- It is deployed from `main` automatically and exists to connect the QA accounts; `/mcp` is not
  reachable from the internet (D22).
- Its Caddyfile, `deploy/qa-runner/Caddyfile` (D22), uses the Origin CA certificate instead of
  ACME.
- `PRIVACYFENCE_TRUSTED_PROXIES` names the Caddy container only. Caddy takes the client address
  from `CF-Connecting-IP` with `trusted_proxies` set to Cloudflare's ranges.
- Cloudflare's Bot Fight Mode is off for the zone, or a WAF skip rule covers
  `/.well-known/privacyfence-relay` and `/oauth/callback/*` on this host. Otherwise the relay's proof
  fetch and the redirects are challenged.
- It runs the image built from the latest `main` (D22), never a hand-loaded one.

**The relay gains a restore route (p11).** A revocation (D5) is permanent today. A mistaken one, or
ma2's revocation test on the long-lived QA host, could not be undone.

- `POST /admin/restore` uses the same token check and body as `/admin/revoke`. It sets
  `revoked = 0` and clears `verified_at`, so the host must prove control again before forwarding
  resumes. It returns 200 `{"host", "revoked": false}`.
- The runbook (`docs/oauth-relay.md`) documents it.
- Tests: 401 without the token; after restore, the callback is refused until a new successful
  `/register`.

**Docs.**

- `docs/connector-qa.md`'s runner section (`:463-585`) is rewritten for this host. It keeps the
  requirements and adds:
  - the Hetzner, Cloudflare and Tailscale setup;
  - the `pfrunner` and Docker separation;
  - the organization-mode QA principal `qa-live`, and connecting its accounts on the QA org
    server in a browser (D22);
  - the QA org server, its deploy from `main` and its blocked `/mcp`;
  - the cutover and decommissioning.
- It fixes the stale "not registered to this repository" line (`:574`).
- `docs/testing-policy.md`, `.claude/commands/qa-record.md`, `.claude/toolkit.yaml`,
  `.claude/skills/steward/SKILL.md`, `.github/pull_request_template.md` and
  `tests/fixtures/live/README.md` change the runner's name and label wherever they give it.
- ADR **0265** (new) records the decision. ADRs 0019 and 0181 get a status line (amended by
  0265). ADR 0019's dead link to `docs/connector-live-check-setup.md` is fixed, as a status-line
  note only.

### D18. Peek and Grist auto-provisioning, which landed on main during planning

**What `main` has.** Peek and Grist auto-provisioning (ADRs 0241-0248): each person who signs in to
an org server gets a Peek account, or a Grist API key, connected for them.

- **Code.** `org_mode.PeekProvisioningConfig` and `GristProvisioningConfig` (`org_mode.py:521-651`),
  `peek_provisioning.py` and `grist_provisioning.py`.
- **Bundle keys.** On `main` its settings are bundle keys, `peek.auto_provision` and
  `grist.auto_provision`:
  - `enabled`;
  - `key_env`, the environment variable that holds the provisioning key;
  - `trust_unverified_email`;
  - for Grist, `provisioner_url`.
  They need `peek.base_url` or `grist.server_url` in the same bundle, and are written by
  `build_org_bundle.py --peek-auto-provision` and `--grist-auto-provision`.
- **Where `daemon_main` reads them.** It validates them at startup (`:1163-1167`) and starts both
  workers (`:1221-1222`). `--peek-release` and `--grist-release` read the raw bundle (`:2386`,
  `:2436`).
- **Released in 6.11.0** (2026-10-27), in the bundle shape above. Servers running 6.11 may have it
  on, so the 7.0 upgrade must keep it working (the import below).

This section overrides D2, D3, D6 and D7 where they differ.

**The split.** The switch is operational, so it moves to the admin config and the Admin page with
the other operational settings. The addresses the provisioning key is sent to are not on the page:
they come from the environment. That keeps the page from pointing the key at another host, the
risk SEC-06 closes for audit forwarding. An admin with a passkey can turn automatic accounts on and
off, but cannot redirect the key.

**Server addresses from the environment (p3).**

| Variable | Replaces | Used for |
|---|---|---|
| `PRIVACYFENCE_PEEK_BASE_URL` | the Admin page's Peek address | Peek connections, and required for Peek automatic accounts |
| `PRIVACYFENCE_GRIST_SERVER_URL` | the Admin page's Grist address (API-key mode) | Grist connections, and required for Grist automatic accounts |
| `PRIVACYFENCE_GRIST_PROVISIONER_URL` | the bundle's `grist.auto_provision.provisioner_url` | required for Grist automatic accounts; it has no Admin-page field |

- **Normalising.** Each address is normalised with `peek_client.normalize_base_url` or
  `grist_auth.normalize_server_url`. A bad value raises `ConfigurationError` at startup, naming
  the variable.
- **Modes.** The variables work in local and org mode. In local mode they only set the address,
  because local mode never provisions.
- **Precedence.** An address from the environment wins over an imported locked address (6.11
  servers, below), which wins over the Admin page's. Either of the first two shows locked on the
  Admin row.
  - `set_server_address` on an environment-locked service is refused with `This address is set by
    PRIVACYFENCE_PEEK_BASE_URL on the server.` (or the Grist variable).
  - A bundle Grist OAuth app still wins over both (D3), and auto-provisioning stays refused with it,
    as on `main`.
- **Reading them.** The function is
  `oauth_apps.environment_addresses() -> dict[str, str]`, with keys `peek`, `grist` and
  `grist_provisioner`; a variable that is unset or empty is left out. It reads the environment on
  each call, and `effective_org_config` calls it.

**Provisioning keys (p20c).**

- `org_mode.provisioning_key(key_env) -> str | None` returns the stripped value of `key_env`, or
  the contents of the file named by `key_env + "_FILE"` (one trailing newline stripped).
- Setting both raises `ConfigurationError("Set <key_env> or <key_env>_FILE, not both.")`. An
  unreadable file raises `ConfigurationError` naming the variable, never the content.
- It replaces the direct `os.environ.get(config.key_env, "")` reads in `peek_provisioning.py:309`,
  `grist_provisioning.py:358` and `daemon_main.py:2393`, `:2443`.

**The admin config (D3).**

- `services.peek` and `services.grist` gain `"auto_provision": {"enabled": false,
  "trust_unverified_email": false, "key_env": "<default>"}`. The defaults are
  `PRIVACYFENCE_PEEK_PROVISIONING_KEY` and `PRIVACYFENCE_GRIST_PROVISIONING_KEY`.
- `key_env` is not editable on the page, as the audit-forwarding `bearer_token_env` is not (ADR
  0256).
- **Bundle keys become legacy (D2).**
  - `peek` stays a top-level legacy key, including `peek.auto_provision`, so `"peek"` is in
    `LEGACY_BUNDLE_KEYS`.
  - `grist.auto_provision` joins the legacy sub-keys, next to the API-key `grist.server_url`.
- **Import (`import_from_bundle`).**
  - It copies `enabled`, `trust_unverified_email` and a non-default `key_env` into
    `services.<s>.auto_provision`.
  - The addresses are imported as any legacy address is, into the admin `base_url`/`server_url`.
  - **A 6.11 server with automatic accounts on.** When the bundle had `auto_provision.enabled`,
    the import keeps the addresses as locked: `services.<s>.address_locked: true`, and Grist's
    `provisioner_url` goes into `services.grist.auto_provision.provisioner_url`.
    - A locked address came from the signed bundle, so it may receive the key, exactly as an
      environment address may. The Admin page cannot edit it: `set_server_address` is refused
      with `This address came from the signed configuration file; set PRIVACYFENCE_PEEK_BASE_URL
      on the server to change it.` (or the Grist variables).
    - An environment variable, when set, wins over a locked address, and that is the only way to
      change it.
    - Automatic accounts keep working through the upgrade with no operator action.
- **Re-import.** The SEC-11 merge never turns `auto_provision.enabled` on or off; the Admin page
  decides.

**The effective config (D3, step 3).** For an offered service:

- **peek.**
  - `base_url` is the environment address, otherwise the admin one.
  - The section also gets `"auto_provision": {"enabled": True, "key_env": ...,
    "trust_unverified_email": ...}` only when all of these hold: the admin config has
    `auto_provision.enabled`; the mode is org and the install is not single-user; and `base_url`
    came from the environment or is an imported locked address.
  - In every other case the section has no `auto_provision`.
- **grist.**
  - A bundle OAuth app gives the bundle section, with no `auto_provision`.
  - Otherwise `server_url` is the environment address, otherwise the admin one.
  - `auto_provision` is added under the peek conditions, with `server_url` and
    `grist_provisioner` both from the environment. It carries `provisioner_url` from
    `PRIVACYFENCE_GRIST_PROVISIONER_URL`.
- **The result.** `PeekProvisioningConfig` and `GristProvisioningConfig` read this composed section
  unchanged, so `main`'s validation and workers need no change.

**Startup and the release commands (p4).**

- **Startup.** The startup validation and `peek_provisioning.start`/`grist_provisioning.start`
  receive the effective config.
  - Turning automatic accounts on or off is a "restart" action (D6), because the workers start
    once.
  - The same holds for withdrawing Peek or Grist: a provisioner already running keeps renewing
    until the restart, and then it does not start.
- **Release commands.** `run_peek_release` and `run_grist_release` build the effective config the
  way `run_app` does, `effective_org_config(bundle, admin_config.config_for(bundle,
  mode=resolve_mode(bundle), single_user=is_single_user(bundle)), mode=resolve_mode(bundle))`,
  instead of reading the raw bundle. The CLI dispatch's local-mode effective config (D4) is not
  used for them.

**The Admin page (D6).**

- **The action.** `set_auto_provisioning`:

  | Parameters | Modes | Sensitive | Applies |
  |---|---|---|---|
  | `service: str` (`peek` or `grist`), `enabled: bool`, `trust_unverified_email: bool` | org | yes (always a passkey, SEC-04) | restart |

  - It is `not_applicable` in single-user mode, where there is no IdP sign-in to provision from,
    and in local mode.
  - Turning it on is refused with the matching line below when its addresses do not come from the
    environment, or when a bundle Grist OAuth app is present.
- **`service_rows`.**
  - For peek and grist, `options` gains `"address_source": "environment" | "imported" | "admin" | "bundle"` and
    `"locked": bool`. Locked means the address source is not "admin".
  - It also gains `"auto_provision": {"enabled": bool, "trust_unverified_email": bool,
    "available": bool, "reason": str, "key_set": bool}`. `key_set` is whether
    `provisioning_key(key_env)` returns a value; it is never the key.
- **Status lines and texts.**
  - `Server: <address> (set by <VARIABLE> on the server).`
  - `Automatic accounts: on. Each person who signs in gets a Peek account connected for them.`
    (Grist: `... gets a Grist key connected for them.`)
  - `Automatic accounts: on, but PRIVACYFENCE_PEEK_PROVISIONING_KEY is not set on the server.`
    (It names `key_env`.)
  - Unavailable, Peek: `Automatic accounts need the Peek address from PRIVACYFENCE_PEEK_BASE_URL on
    the server, so the provisioning key only goes where the server's operator points it.`
  - Unavailable, Grist: `Automatic accounts need PRIVACYFENCE_GRIST_SERVER_URL and
    PRIVACYFENCE_GRIST_PROVISIONER_URL on the server.`
  - With a bundle OAuth app: `Automatic accounts work only with API keys; this server uses a Grist
    OAuth app.`
  - The trust checkbox: `Trust email addresses the identity provider does not mark verified (only
    for providers such as Microsoft Entra ID that issue only verified, organization-owned
    addresses).`
- **Pausing.** Withdrawing the service hides the switch and, after a restart, stops provisioning.

**Bundle tooling (D7).** `--peek-auto-provision`, `--no-peek-auto-provision`,
`--peek-provisioning-key-env`, `--peek-trust-unverified-email`, `--grist-auto-provision`,
`--no-grist-auto-provision`, `--grist-provisioner-url`, `--grist-provisioning-key-env` and
`--grist-trust-unverified-email` join D7's hidden, refused legacy flags. The refusal text names the
Admin page and the environment variables. `main`'s builder tests for them become refusal tests.

**Single-user mode (D9).** Provisioning starts from the IdP sign-in callbacks
(`routes_org_identity.py:228-231`, `oauth_provider.py`), and single-user sign-in (D10) never calls
them. On top of that, the effective config never carries `auto_provision` there. p16 adds a test
that a single-user sign-in calls neither `notify_sign_in`.

**Docker and docs (p20c, p13a, p13b).**

- **Docker.**
  - `deploy/docker/privacyfence.env.example` gains a commented "Peek and Grist (optional)" block
    with the three address variables and `..._PROVISIONING_KEY_FILE`.
  - `compose.yaml`'s commented `secrets:` example gains `peek_provisioning_key` and
    `grist_provisioning_key`.
- **Docs.**
  - `peek-setup.md` and `grist-setup.md` describe automatic accounts as: set the address variables
    and the key on the server, then turn them on under Admin → Services.
  - The configuration reference moves the provisioning rows from the bundle flags to the admin
    config and the environment.

**Unverified email is handled one way.** Main's provisioning trusts an unverified email only with
an explicit `trust_unverified_email`, for providers like Microsoft Entra ID that never send
`email_verified`. SEC-07 follows the same pattern, so organizations using Entra ID with allowed
domains are not locked out:

- `idp.trust_unverified_email` (bundle), or `PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL=true`
  (environment, D14), lets `check_authz_policy` trust the email domain without
  `email_verified`.
- The Google `hd` rule still applies, and admins by email never use it: they always need a
  verified address.
- The docs and the CHANGELOG line name it as the fix for Entra ID organizations that use allowed
  domains.

**Line references.** Every `file:line` in this plan was refreshed against `main` at `95e69b2d`
(after the Peek, Grist and 6.10.0 merges). 6.12.0's scoped token proposals (D21) have since shifted
lines in `routes_settings.py`, `settings_window_html.py`, `scoped_tokens.py`, `mcp_dispatch.py` and
`build_org_bundle.py` by up to a few hundred; the references D21 and p18 give are from `1b0985dd`.
Phases locate code by the names given next to each reference, never by the line alone.

### D19. Built-in apps are for organization mode only, and the Slack Marketplace listing

**Organization mode only.** Desktop (local) mode is deprecated in 7 and removed in 8 (D11), so it
gets no built-in apps.

- Built-in apps serve organization servers and single-user installs (D9), always through the
  relay. Desktop installs keep working exactly as in 6.x: a provider is available only when the
  bundle carries the organization's own app.
- **Five apps, all on the relay.** Every built-in app has only `https://oauth.privacyfence.eu/...`
  redirects. Slack, Miro and Atlassian all refuse to distribute an app that has an `http` redirect,
  so this is what lets their apps be distributed. Google needs only a Web client.
- **Desktop installers ship no built-in credentials (SEC-13).** `build_dmg.sh`, `build_deb.sh` and
  `build_installer.ps1` do not call `scripts/builtin_apps.py write`. The org image and the PyPI
  wheel do, because they run servers.
- **The deprecation notice says what desktop users miss (D11).** `LOCAL_MODE_DEPRECATION` gains
  the sentence: `PrivacyFence's built-in sign-in for Google, Slack, Salesforce, Atlassian and Miro
  works only in organization mode and the Docker image.`
- **Tests.** In local mode, `credentials_source` never returns `"builtin"`, even when
  `builtin_app` returns a value.
- **The live-check runner (D17).** It runs in organization mode as the principal `qa-live`, with the
  built-in apps. Its tokens are minted on the QA org server at `runner.privacyfence.eu` and moved
  to the runner, so nothing changes when 8.x removes local mode.
- **What this replaces.** It reverts the earlier idea of sending desktop Slack sign-ins through the
  relay. There is no `LOCAL_VIA_RELAY`, no `relay_grant` in `oauth_loopback`, and no phase for it.

**The Slack Marketplace listing (manual step ma9).**

- **The limit.** A distributed Slack app that is not in the Slack Marketplace gets 1 request per
  minute and 15 messages per call on `conversations.history` and `conversations.replies`
  (`docs/slack-setup.md:28-33`). Until PrivacyFence is listed, every server using the built-in
  Slack app gets slow, truncated channel and thread reads.
- **The listing.** It needs:
  - the short and long descriptions;
  - icons and screenshots;
  - a landing page (`https://privacyfence.eu/connectors/slack/`);
  - the privacy policy;
  - a support contact;
  - Slack's review.

  Slack's current requirements decide when it can be submitted, such as a minimum number of active
  installs. ma9 checks them.
- **Until it is listed:** D24 replaces the line below with limited mode (search for history reads)
  and the one-click own app.
  - **Admin page.** The built-in Slack row's status line adds `Slack limits channel and thread
    reads for apps outside the Slack Marketplace until PrivacyFence is listed. Your own internal
    Slack app avoids it.`
  - **The flag.** The line is driven by `oauth_apps.SLACK_MARKETPLACE_LISTED: bool = False`. A
    one-line PR flips it after approval, and the next release drops the line.
  - **Docs.** `slack-setup.md` and the 7.0 release notes say the same.

### D20. Built-in Google sign-in, opt-in

**What it is.** An organization server may use PrivacyFence's own Google OAuth client as its IdP.
That makes the env-only bootstrap (D14) register no app at all: DNS, `PRIVACYFENCE_ISSUER_URL`,
`PRIVACYFENCE_IDP=builtin-google`, `PRIVACYFENCE_ADMIN_EMAILS`, then `docker compose up -d`. It
is **opt-in**: nothing switches it on without that setting, and a bundle or environment IdP of the
organization's own always wins.

**The client.**

- It lives in its own Google Cloud project, `privacyfence-signin`, separate from
  `privacyfence-builtin`. A suspension or abuse report against one project then cannot take down
  both sign-in and the Gmail and Drive connectors.
- It is a Web client with audience External, In production, and the scopes `openid email profile`
  only. Those are not sensitive, so there is no 100-user cap and no security assessment. Brand
  verification still applies (ma8).
- Its redirect URIs are on the relay:
  - `https://oauth.privacyfence.eu/callback/idp`
  - `https://oauth.privacyfence.eu/callback/idp-login`
  - `https://oauth.privacyfence.eu/callback/stepup`
- It is the sixth built-in key, `google_signin`, with the repository secrets
  `PF_APP_GOOGLE_SIGNIN_CLIENT_ID` and `..._SECRET` (p1, mb1). Like the others, it ships only in
  the org image and the PyPI wheel.

**Configuration (`org_mode.idp_section`, D14).**

- `PRIVACYFENCE_IDP=builtin-google`, or a bundle `"idp": {"builtin": "google"}`, gives the section
  `{"issuer": "https://accounts.google.com", "client_id": <built-in>, "client_secret":
  <built-in>, "relay": true, "builtin": true, "admin_emails": [...], "trust_unverified_email":
  false}`.
- **`ConfigurationError` cases:**
  - Built-in Google combined with `PRIVACYFENCE_IDP_ISSUER`, `..._CLIENT_ID` or
    `..._CLIENT_SECRET[_FILE]`: `PRIVACYFENCE_IDP=builtin-google cannot be combined with your own
    IdP settings.`
  - No admin email: `The built-in Google sign-in needs PRIVACYFENCE_ADMIN_EMAILS.`
  - The built-in `google_signin` app is absent from this build: `This build has no built-in
    Google sign-in; use your own IdP app.`
  - An `http://` or loopback issuer: `The built-in Google sign-in needs a public https://
    server address.` Single-user mode remains the localhost path.
- SEC-14 still holds: a legacy bundle's `idp` section wins over the environment setting.

**Who may sign in: default deny.** With the built-in client, any Google account in the world can
complete Google's side, so the server decides alone. `org_identity.check_authz_policy`, when
`idp.builtin` is true:

- **Admins.** Admins named in `admin_emails` may sign in. SEC-07 applies: `email_verified` must
  be true, and the `hd` claim must equal the email's domain. That domain must also be DNS-verified
  (below).
- **Allowed domains.** People whose verified email is in an allowed domain may sign in, with `hd`
  equal to that domain. The domains come from the admin config's `authz.allowed_domains`, edited
  on Admin → Sign-in. `PRIVACYFENCE_ALLOWED_DOMAINS` (comma-separated) seeds them on the first
  import (D3), and is ignored afterwards with a log line.
- **Everyone else** is refused with the existing denial page, even when `authz` is empty. Without
  the built-in client, an empty `authz` keeps today's meaning.
- **No `trust_unverified_email`.** The flag cannot be set with the built-in client (D18).
  Personal Gmail accounts have no `hd`, so they can never sign in this way. The docs say so: use
  your own IdP app for them.
- **Admin rights.** The group claim is unavailable (Google sends no groups), so admins are named
  by email only.

**Through the relay.**

- **The Worker (p11).** Three grants with fixed paths:
  - `idp` → `/oauth/idp/callback`
  - `idp-login` → `/oauth/idp/login-callback`
  - `stepup` → `/oauth/stepup/callback`

  They follow the same host rules, registration check and parameter rules as the connector
  grants. A loopback host is refused for them (400 `invalid_request`).
- **The server.** The three places that build an IdP `redirect_uri` use
  `oauth_apps.relay_redirect_uri(<grant>)` and a `host~nonce` state (D5) when `idp.relay` is true:
  `daemon_main` (`idp_callback_url`, `:1230`), `routes_org_identity` (`LOGIN_CALLBACK_PATH`) and
  `routes_org_stepup` (`:144`, `:169`). The pending attempt stores that `redirect_uri` for the code
  exchange.
- **Registration.** `relay_client.status()["needed"]` is true from the first start when the IdP is
  built-in. Registration then starts at once, and the well-known token is served (SEC-09).
- **Before registration succeeds,** `/login` shows `Sign-in is starting up: this server is
  registering with the PrivacyFence relay. Try again in a minute.` It does not send the person to
  Google.

**Domain proof for sign-in (amends D5 and SEC-23).** Host proof shows that the operator controls
the server's name. It does not show which organization's Google accounts that server may sign
in. So a server using the built-in sign-in must also prove each Google Workspace domain it signs
people in from, and the relay enforces it. The same registration, notification email and
maintainer revocation then cover sign-in, as for the connectors.

- **Which domains.** The sign-in domains are the domains of `admin_emails` plus
  `authz.allowed_domains`, lower-cased and de-duplicated. `relay_client.register` sends them as
  `"signin_domains": [...]` (at most 20) in the `/register` body. It does this only when the IdP
  is built-in.
- **The proof: one DNS TXT record per domain.** It goes at `_privacyfence-relay.<domain>`, with the
  value `pf-relay=<hex sha256 of the server's relay token>`.
  - The token is already public at `/.well-known/privacyfence-relay`, so its hash reveals nothing.
  - DNS proves control of the domain itself. A server at `pf.acme.com` does not prove
    `acme.com` by its host name alone: a subdomain can belong to someone else, such as a SaaS
    vendor's customer.
- **The Worker** checks each domain with DNS over HTTPS
  (`https://cloudflare-dns.com/dns-query?name=_privacyfence-relay.<domain>&type=TXT`,
  `accept: application/dns-json`) on every successful `/register`.
  - It stores the verified set in a new `signin_domains` column, a JSON array, on the
    registration row.
  - It answers with `"signin_domains_verified": [...]` and
    `"signin_domains_missing": [{"domain", "txt_name", "txt_value"}]`.
  - A domain whose record disappears drops out at the next daily re-registration.
  - Domains that fail the host rules' syntax are refused with 400 `invalid_request`.
  - A PSL public suffix is refused too, for example `gmail.com` or `co.uk` alone.
- **The relay enforces it on every sign-in.** The Worker forwards a code on `idp`, `idp-login` or
  `stepup` only when the redirect's `hd` query parameter is in the host's verified
  `signin_domains`.
  - Google appends `hd` to the redirect for Google Workspace accounts.
  - A personal Google account has no `hd`, so it is refused here too.
  - Otherwise the Worker returns 403 `text/plain`: `This Google account's organization is not
    allowed to sign in to this PrivacyFence server.`, and the code is never forwarded.
  - A redirect that carries `error` and no `code` (the person cancelled, or Google refused) is
    forwarded without the `hd` check, so the server can show its own error page; it carries
    nothing to sign in with.
  - So a hostile or modified server can sign in only accounts of domains it proved in DNS. It
    cannot use PrivacyFence's sign-in client against anyone else's users. This bounds the sign-in
    client only: a registered server can still ask any Google account to consent to the connector
    client, `google_web` (SEC-02), which notification, review and revocation cover.
  - **The kill switch.** The `hd` parameter is observed behaviour, not a documented contract. The
    Worker variable `SIGNIN_REQUIRE_HD` (default `"true"`) can turn the relay-side check off if
    Google ever stops sending it. The server-side check below still holds, and the runbook names
    this switch.
- **The server enforces it as well.** With the built-in IdP, `check_authz_policy` allows a sign-in
  only when the account's `hd` is in the verified set from the last successful registration.
  `relay_client` persists that set in `org/relay_status.json`. This is on top of D20's default-deny
  rules.
- **Bootstrap.** Admins cannot sign in before their domain is proven, so the server says what to
  add without needing the Admin page.
  - At every start, and after each registration, it logs one line per missing domain:
    `Built-in Google sign-in: add the DNS TXT record _privacyfence-relay.acme.com with the value
    pf-relay=<hash>. Until then nobody from acme.com can sign in.`
  - `privacyfence-app --relay-dns-records` prints the same lines and exits.
  - The `/login` page shows `Sign-in is waiting for DNS: the administrator must publish the
    record shown in the server log.` while no domain is verified.
  - The Admin → Sign-in section lists each domain as verified or missing, with its record.
- **The notification email (SEC-23)** for a new registration, and for a registration whose
  verified sign-in domains change, says `Built-in Google sign-in: yes` and lists the verified
  domains. That is the prompt to reject a server signing in a domain it should not.
- **Revocation.** `/admin/revoke` stops a host's connectors and sign-in together. Rejecting is one
  command, as for the connectors.

**Cross-server safety.** Every server shares one client, so Google issues sign-in tokens addressed
to all of them alike. A server must accept only a sign-in it started:

- the code flow with PKCE and `nonce` stays mandatory (`org_identity.py:196-260`);
- the server never accepts an ID token that it did not obtain from its own code exchange;
- `state` is single-use and bound to the pending attempt.

p20e's tests cover each point:

- a code issued for another server's PKCE challenge fails the exchange;
- an ID token whose `nonce` belongs to another attempt is refused;
- a replayed `state` is refused.

**Step-up.** The IdP step-up for approvals goes through the same client and the `stepup` grant.
Passkey step-up is unaffected.

**Admin page (D6).** The Sign-in section's first line reads `Sign-in: PrivacyFence's built-in
Google sign-in, through oauth.privacyfence.eu. Only the admins below and people in the allowed
domains can sign in.` It lists `admin_emails` read-only (they come from the environment).

**Privacy and terms (D16, p13c).**

- The relay section of the privacy page adds that servers using the built-in Google sign-in send
  their sign-in redirects through the relay. The relay sees a one-time code and the account's
  Workspace domain (`hd`), and stores each server's DNS-proven sign-in domains with its
  registration.
- The relay terms add that the sign-in client may be suspended or withdrawn like the rest.

**Risks.**

- **Every login depends on the relay and the `privacyfence-signin` project.** If either is down
  or suspended, nobody can sign in to any server using it. Passkey sign-in for existing sessions
  is not a fallback, because sign-in itself is the IdP. The docs present the built-in sign-in as
  the quick start for small Google Workspace teams. They recommend an organization's own IdP app
  for anyone who needs Workspace-level control (an Internal app, groups) or independence from
  PrivacyFence's services.
- **Brand misuse of the sign-in client is bounded by the domain proof.** A malicious operator
  could still show "PrivacyFence" on its own server's Google sign-in page. But the relay forwards a
  sign-in code only for accounts of domains that operator proved in DNS, so it cannot harvest
  anyone else's identities through `google_signin`. Registration emails list those domains, and
  revocation stops the server. The connector clients are not bounded this way: any registered
  server can ask any account to consent to them, which is SEC-02's accepted risk. Requiring DNS
  proof for connector grants too was considered and not adopted, because organizations with their
  own IdP connect Google accounts from domains they need not prove, and single-user installs
  connect personal accounts.
- **The relay token anchors the DNS records.** Every TXT value is the hash of
  `org/relay_token.txt`, so losing that file without a backup means publishing new records
  (SEC-26).
- **The relay-side check relies on Google's `hd` redirect parameter.** If Google drops it, built-in
  sign-ins fail at the relay until a maintainer sets `SIGNIN_REQUIRE_HD = "false"`. The server-side
  `hd` check from the ID token still applies.

### D21. AI-proposed scoped tokens, which landed on main during planning

**What `main` has (6.12.0, ADRs 0250-0253).** An AI client can propose a scoped token with the
`privacyfence_propose_scoped_token` meta-tool, and a person reviews and creates it.

- In organization mode it is off until the bundle sets `scoped_tokens.allow_ai_proposals: true`
  with scoped tokens enabled (ADR 0253, `scoped_tokens.policy_from_org_config`, `:187-201` on
  `1b0985dd`).
- `build_org_bundle.py` writes it with `--enable-scoped-token-proposals` and
  `--disable-scoped-token-proposals`.
- The policy is read once at startup (`daemon_main.py` ~:1240, `web/server.py` ~:1400).
- Its refusal text is `scoped_tokens.PROPOSALS_DISABLED_MESSAGE`, which names the bundle.

This section overrides D3, D6 and D7 where they differ.

**The split.** `allow_ai_proposals` is a key of the `scoped_tokens` section, so it moves with that
section (D2, D3): imported once with it, then read from the admin config. `policy_from_org_config`
reads the composed section unchanged, so `main`'s rule that proposals need scoped tokens enabled
holds.

- **Admin page (D6).** `set_scoped_tokens` gains `allow_ai_proposals: bool`. It is stored as given;
  the policy still requires `enabled`. The summary stays `Changed scoped tokens`. The action stays
  sensitive and applies on restart.
  - The Scoped tokens form (p9b) gains a checkbox, `Let AI clients propose a scoped token for a
    person to review and create.` While `enabled` is off, it shows `Needs scoped
    tokens turned on.`
- **Bundle tooling (D7).** The two flags join the refused legacy flags.
- **Single-user mode.** `import_from_bundle` leaves `scoped_tokens` empty there, as on a new org
  server, so tokens and proposals start off. Turning them on is an Admin-page change with a
  passkey.
- **Local mode** is unchanged: `policy_from_org_config(..., org_mode=False)` always allows them.
- **Messages (p18).** `PROPOSALS_DISABLED_MESSAGE` names the Admin page.
- **ADR.** 0256 amends 0253: the switch lives in `org/admin_config.json` and on the Admin page.

### D22. The QA server is deployed from main, and its accounts are connected in a browser

This section overrides D17 where they differ.

**What it is.** `https://runner.privacyfence.eu` runs the organization server built from the latest
`main`, redeployed automatically. It exists so the maintainer can connect the QA accounts in a
browser, which organization mode makes possible for every service. `/mcp` and every MCP OAuth
endpoint are blocked at its proxy. The live checks copy the QA principal's tokens from its data
directory.

**Why `/mcp` is blocked.**
- Nobody can point an AI client at a public server holding live QA tokens by accident.
- With no MCP, nothing on the server ever builds the QA principal's connectors, so nothing there
  uses or refreshes its tokens. The runner is then their only user. That matters for Atlassian's
  rotating refresh tokens and for Telegram, whose session must not be used from two places at once.

**The deploy: `.github/workflows/deploy-qa-server.yml` (p22).**
- **Triggers.** A push to `main` touching `src/**`, `deploy/docker/**`, `deploy/qa-runner/**`,
  `pyproject.toml`, `setup.py`, `MANIFEST.in`, `requirements/**` or the workflow itself; and
  `workflow_dispatch` on `main`. `concurrency: {group: qa-server-deploy, cancel-in-progress:
  true}`. The runner runs one job at a time, so a deploy never overlaps a live check on the host.
- **Job `build`** (`ubuntu-latest`): the same steps as `build.yml`'s `build-org-image` for amd64:
  - write the Telegram and built-in app credentials, with the secrets in those steps' `env:` only;
  - build the wheel and the image;
  - tag the image `privacyfence-org:qa-main`;
  - upload it with `docker save | gzip` as the artifact `qa-server-image`, kept for 3 days.
- **Job `deploy`** (`needs: build`, `runs-on: [self-hosted, privacyfence-live]`): downloads the
  artifact to `$HOME/qa-deploy/privacyfence-org-qa.tar.gz`, then runs
  `sudo -n /usr/local/sbin/pf-qa-deploy`. It checks out nothing and holds no secret.
- **Job `verify`** (`needs: deploy`, `ubuntu-latest`, so from the public internet):
  - `/login` answers 200 or a redirect;
  - `/mcp`, `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`,
    `/authorize`, `/register`, and a POST to `/token`, each answer 404.

**On the host: `deploy/qa-runner/` (p22), installed once by hand (ma6).**
- **`pf-qa-deploy`**, a POSIX shell script installed root-owned, mode 0755, at
  `/usr/local/sbin/`. It takes no arguments.
  - It reads only `/home/pfrunner/qa-deploy/privacyfence-org-qa.tar.gz`, refusing a symlink or a
    non-regular file, and checks it with `gunzip -t`.
  - It runs `docker load`, and refuses unless the loaded reference is `privacyfence-org:qa-main`.
  - It runs `docker compose --project-directory /opt/pf-qa/run up -d --no-build`, waits up to 120 s
    for the `privacyfence` service to report healthy, and fails the job otherwise.
  - Then `docker image prune -f`, and it deletes the tarball.
- **`sudoers.pf-qa-deploy`**: `pfrunner ALL=(root) NOPASSWD: /usr/local/sbin/pf-qa-deploy ""`. The
  empty argument list forbids any argument.
- **`compose.override.yaml`**, used with `deploy/docker/compose.yaml`:
  - `privacyfence`: `image: privacyfence-org:qa-main`, `pull_policy: never`, `user: "10001:10001"`,
    the data directory as a bind mount of `/home/pfrunner/qa-org-data` (in place of the named
    volume), and the `idp_client_secret` secret.
    - The compose file, not the image, fixes the user, the read-only root, the dropped
      capabilities and `no-new-privileges`. So an image built from `main` cannot run as root on
      the host.
  - `caddy`: this directory's Caddyfile and `/etc/pf-qa` (the origin certificate), read-only.
  - `peek`: `ghcr.io/privacyfence/peek:main` on the same network, with its own volume and
    `PEEK_BASE_URL=https://peek-qa.privacyfence.eu`.
- **`Caddyfile`**:
  - global `trusted_proxies static {$PF_CF_RANGES}` and `client_ip_headers CF-Connecting-IP`;
  - the site `runner.privacyfence.eu`: the origin certificate, then a matcher `@mcp` answering 404
    for `/mcp`, `/mcp/*`, `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/idp/callback`,
    `/oauth/consent/*`, `/.well-known/oauth-authorization-server*` and
    `/.well-known/oauth-protected-resource*`, then `reverse_proxy privacyfence:8765`;
  - the site `peek-qa.privacyfence.eu` → `peek:7700`. Peek needs a public `https` address,
    because its device sign-in opens in the maintainer's browser and its addresses must be `https`
    or loopback (`peek_client.normalize_base_url`).
- **`privacyfence.env.example`**: `PRIVACYFENCE_ISSUER_URL`, the QA IdP variables,
  `PRIVACYFENCE_ADMIN_EMAILS=test@privacyfence.eu`, `PRIVACYFENCE_TRUSTED_PROXIES=172.30.87.2`,
  `PRIVACYFENCE_PEEK_BASE_URL=https://peek-qa.privacyfence.eu`,
  `PRIVACYFENCE_GRIST_SERVER_URL=https://docs.getgrist.com` and `PRIVACYFENCE_PREVIEW_SERVICES=*`.
- **`README.md`**: what each file is, and how ma6 installs them.
- **Defence in depth (mb4).** A Cloudflare WAF custom rule blocks the same paths on
  `runner.privacyfence.eu`, so `/mcp` stays closed even if the Caddyfile is changed by mistake.

**Users and separation (amends D17).**
- `pfrunner` is created with uid and gid 10001, the image's `privacyfence` user. The server's data
  directory, `/home/pfrunner/qa-org-data` (mode 0700), is therefore owned by `pfrunner` on the host.
- That one directory is shared on purpose: the server writes the tokens, and the runner reads them.
  The server sees nothing else of `pfrunner`'s.
- `pfrunner`'s one sudo rule runs the fixed compose project and nothing else.
- ADR 0265 records this: the runner can change the QA server's data, and the QA server can change
  the tokens the runner uses. Both hold only QA accounts.

**Connecting the QA accounts (ma6), all in a browser.**
- Sign in at `https://runner.privacyfence.eu` as `test@privacyfence.eu`. It is the QA server's
  only admin and the account whose tokens the runner uses.
- Offer every service on Admin → Services. Then, on `/connect`:
  - Google (all six), Slack, Salesforce, Atlassian and Miro, through the relay with the built-in
    apps;
  - Telegram with the phone number and code;
  - Grist with its API key;
  - Peek with the device sign-in at `peek-qa.privacyfence.eu`.
- Re-minting a token is reconnecting it on `/connect`.

**How the runner uses them (p21, with `scripts/qa_runner_tokens.py` from p22).**
- **`pull`** finds the QA principal's folder under `/home/pfrunner/qa-org-data/users/`:
  - the folder named by the repository variable `PF_QA_PRINCIPAL_ID` when it is set;
  - otherwise the single folder whose `credentials/` holds a `daemon_main.TOKEN_FILES` file.
  - With none, or several, it fails and names the candidate ids. Principal ids are not secrets.
  - It copies that `credentials/` to `<checkout>/users/qa-live/credentials/` (files mode 0600),
    and records each file's SHA-256 in `<checkout>/.qa-pull.json`.
- **`push`** (`if: always()`) writes back only the files the run changed, each atomically (a
  temporary file in the same directory, then a rename). It skips, with a warning, any file whose
  server copy changed since `pull`, for example one the maintainer reconnected during the run.
- `tests/fixtures/qa_environment.yaml` stays a hand-placed configuration file in
  `~/privacyfence/` (seed ids, no credential).
- Tests (`tests/unit/test_qa_runner_tokens.py`): the variable and the single-folder rule; none and
  several fail with the ids; the mode; push writes only changed files; push keeps a file changed on
  the server; a symlinked file is refused.

**A new connector is two pull requests.** Live recording needs a token, and the token now comes
from `/connect` on the QA server, which runs `main`. So a new connector lands in two PRs:

1. **Connect.** The sign-in and token storage, the `/connect` entry, a built-in app key when the
   provider needs one (with its manual "register the app, add the secrets" step), and unit tests.
   The service is listed in `oauth_apps.PREVIEW_SERVICES`.
   - After the merge, the QA server redeploys. The maintainer connects the QA account on `/connect`
     and seeds the data `docs/connector-qa.md` describes.
2. **Tools.** The connector's tools, recorded live with `/qa-record <connector>` (the token is now
   on the QA server), the live-check manifest entries, and removing the service from
   `PREVIEW_SERVICES`.

A release cut between the two never shows a half-built service.

- **`oauth_apps.PREVIEW_SERVICES: frozenset[str] = frozenset()`** (p22). `effective_org_config`
  drops a preview service's section, and `admin_config.service_rows` omits its row, unless
  `PRIVACYFENCE_PREVIEW_SERVICES` (a comma-separated list, or `*`) names it. The QA server sets
  `*`.
- **Where it is written down.** The procedure is in `docs/coding-and-testing-guidelines.md` §3
  "Adding a connector" and `docs/connector-qa.md`. `CLAUDE.md` gains a short "New connectors"
  line pointing there, so a `/make-plan` for a new connector produces the two PRs, in that order,
  with the browser connection between them as a manual step.

### D23. 7.0 is tested as an alpha release, installed the regular way

**No manual step tests a branch build.** The PR merges on CI and review. Then, in order:

1. The relay deploys from `main` through its workflow (ma0), and the website pages deploy.
2. The QA server deploys from `main`. The maintainer connects the QA accounts in a browser (ma6),
   and the live check runs on the new runner (ma7).
3. The maintainer cuts `7.0.0a1` with `/cut-release` (ma10).
4. The remaining checks install the alpha exactly as an operator or a person would (ma2, ma3, ma4,
   ma1, ma8):
   - the image `ghcr.io/privacyfence/privacyfence-org:7.0.0a1`, through `compose.yaml` with
     `PRIVACYFENCE_IMAGE_TAG=7.0.0a1`, or through `docker run`;
   - the desktop pre-release installers from the download page (ADR 0024).
5. Each defect is fixed in a normal PR to `main`, and the next alpha follows. 7.0.0 is cut once
   every check passes (ma11). The Slack listing (ma9) and the website follow-up (ma5) come after.

**Pre-release org images (p23).** ADR 0208 publishes only stable images, so today an alpha has none.

- `scripts/org_image_tags.py`: a pre-release version (PEP 440 short form, `X.Y.ZaN`, `bN` or
  `rcN`) gets exactly one tag, the version itself. It never moves `X.Y`, `X` or `latest`. Stable
  versions are unchanged, and the floating-tag comparison still ignores pre-release tags.
- `publish-org-image.yml` publishes every channel. A pre-release pushes the version tag (and the
  per-architecture tags) with the same provenance attestation, and its summary says that no
  floating tag moved.
- `build.yml`'s `build-org-image` and `publish-pypi.yml` pass `--require` to
  `scripts/builtin_apps.py check-dist` on every release tag, pre-releases included, because an
  alpha image without the built-in apps could not be tested (p1). Telegram's rule is unchanged.
- `docs/org-mode-docker.md`'s "Releases" row and `docs/releasing.md` say: a stable release gets the
  version, `X.Y`, `X` and `latest`; a pre-release gets only its exact version, so the floating tags,
  and with them unattended upgrades, never reach a pre-release.
- ADR 0267 (new) amends 0208. It keeps 0208's reason (`latest` never points at a pre-release) and
  drops its consequence that a pre-release image must be built by hand.
- Tests: `tests/unit/test_org_image_tags.py` (a pre-release gets exactly `[version]`, even when it
  is the highest tag) and `tests/unit/test_org_image_workflows.py` (no step is skipped for a
  pre-release; the builtin check passes `--require` on every tag).

**Consequences.**
- The relay, its terms and the privacy text go live at the merge, before any 7.x release exists. No
  server uses the relay until the alpha.
- Between the merge and 7.0.0, `main` is 7.0, and `scripts/tag_release.py` tags only `main`'s
  tip. A 6.x bugfix release would need its own maintenance branch, which the repository does not
  have today. So the alpha phase should be short. This is an open question for the maintainer
  (Risks).
- Fixes found on the alpha are fixed forward on `main`; nothing is reverted to retest a branch.

### D24. Slack: the built-in app by default, search for throttled reads, and a one-click own app

This section overrides D19's Slack Marketplace part where they differ.

**The constraint.** Slack limits a distributed app that is not in the Slack Marketplace to 1
request per minute, and 15 messages per call, on `conversations.history` and
`conversations.replies`. The Marketplace accepts a submission only once the app has 10 installs on
active workspaces, counted across every PrivacyFence server's users. 7.0 will not have them.
Internal apps, installed only in the workspace that created them, keep Tier 3 (50 per minute).

**The decision.** The built-in Slack app stays, so Slack connects with no setup like the other
providers. While it is throttled, the connector avoids the two throttled methods where it can, and
the Admin page offers a one-click switch to the workspace's own internal app at full speed.

**Limited mode (p24).** `oauth_apps.slack_limited(section) -> bool` is true when the Slack section
is the built-in app and `SLACK_MARKETPLACE_LISTED` is false.

- **Channel history.** In limited mode `slack_get_channel_history` reads through `search.messages`
  (Tier 2, 20 per minute, up to 100 matches a page). The query is the channel plus the time range,
  for example `in:<#C123> after:2026-11-01`.
  - The result says `Read through Slack search, because Slack limits history reads for this app;
    an administrator can switch to your workspace's own app on the Admin page.`
  - Search does not return everything history does: some message subtypes are missing, there is a
    short indexing delay, and thread replies come back as top-level matches. The tool description
    says so in limited mode.
  - Stop condition: if search cannot be scoped to a channel id, use the channel's name from
    `conversations.info` (Tier 3, unthrottled). If neither works, keep history and say why.
- **Thread replies** have no good substitute. `slack_get_thread_replies` keeps
  `conversations.replies` and reads at most 15 replies. In limited mode the result names the limit
  and the own-app switch.
- **Single-message lookups** (permalinks, and thread previews on approval cards) use the throttled
  methods. In limited mode an approval card shows the message without its thread preview instead of
  waiting up to a minute. A permalink lookup tries `search.messages` first.
- **Routines** already read Slack through search (`docs/slack-setup.md`, "Rate limits and
  routines"), so they are unaffected.

**One-click own app (p24).** The Slack row on Admin → Services, when it uses the built-in app,
shows `Slack limits channel history and thread reads for apps outside the Slack Marketplace. Your
workspace's own app reads at full speed.` and a button **Create your workspace's own Slack app**.

- The button opens `https://api.slack.com/apps?new_app=1&manifest_json=<url-encoded manifest>`.
  The manifest comes from `oauth_apps.slack_manifest(issuer_url) -> dict`:
  - `display_information.name` "PrivacyFence";
  - `oauth_config.redirect_urls`: `[<issuer_url>/oauth/callback/slack]`;
  - `oauth_config.scopes.user`: `slack_client.DEFAULT_USER_SCOPES`, and no bot scopes;
  - `settings`: `org_deploy_enabled: false`, `socket_mode_enabled: false`,
    `token_rotation_enabled: false`.
  - Stop condition: if Slack's create page no longer takes `manifest_json`, show the manifest in a
    copyable block with "Create New App → From an app manifest" instead.
- Left undistributed, that app is internal, so it keeps Tier 3. The row's next two lines say what
  to do next: copy the Client ID and Client Secret from Basic Information, sign them as a connector
  config (D25), and upload the file on this row.
- An internal Slack app works only in the workspace that created it. An organization with several
  Slack workspaces keeps the built-in app, or uses an Enterprise Grid org-wide app; the docs say so.
- Switching makes everyone who connected Slack connect again. The row says so, as for the other
  providers.

**Testing.** The live checks keep the built-in Slack app, so the default path, search fallback
included, is what the weekly check tests. The own-app path is covered by unit tests and ma2.

**The Marketplace listing (ma9)** stays the way out. When it is approved,
`SLACK_MARKETPLACE_LISTED` flips and limited mode ends.

ADR 0269 records this.

### D25. Organization servers have no bundle; own apps are signed connector configs

This section overrides D2, D4, D7, D13 and D14 for organization mode, including single-user mode,
where they differ. Desktop (local) installs keep the bundle exactly as D2, D7 and D13 describe,
until 8.x removes desktop mode.

**What an organization server reads.**

| What | From |
|---|---|
| `mode`, the server address, the identity provider | the environment (D9, D14), or, read-only until 8.x, an upgraded server's legacy bundle |
| an organization's own app for a provider | a signed **connector config**, uploaded on Admin → Services |
| a provider with no connector config | the built-in app (D1), or nothing for Grist and Peek |
| everything operational | the admin config (D3) |

**Connector configs.**

- **One file per provider.** The providers are `google`, `slack`, `salesforce`, `atlassian`,
  `miro` and `grist`.
- **The format is signed text, not JSON,** so that Python and a browser produce exactly the same
  bytes to sign:

  ```text
  privacyfence-connector-config v1
  provider: slack
  serial: 3
  client_id: 1234.5678
  client_secret: ...
  signed_at: 2026-11-02T10:00:00Z
  key: <base64 Ed25519 public key>
  signature: <base64 Ed25519 signature>
  ```

  - The signed message is every line before `signature:`, each ending in `\n`, as UTF-8.
  - Fields come in exactly this order. Salesforce adds an optional `login_url` before
    `signed_at`; Grist adds `auth_server_url` and `server_url` there.
  - Each value is printable ASCII without spaces at either end, at most 512 characters, with no
    control character. A file is at most 8 KB.
  - Field names stay those of the bundle sections (`consumer_key` and `consumer_secret` for
    Salesforce), so the effective config is the same dict the connectors read today.
- **The key** is Ed25519, the same kind `build_org_bundle.py` uses. An organization's existing
  bundle signing key works unchanged.
  - The first upload pins its key (TOFU) at `org/connector_config_signing_pubkey.txt`. The upload
    asks for confirmation first, showing the key's fingerprint, as the bundle upload's 409 does.
  - An upgraded server starts with its legacy bundle's pinned key as this pin, so IT keeps its key.
  - A different key is refused. Rotation means deleting the pin file on the server, as for bundles,
    and the docs say so.
- **Serials.** Each provider's serial must be higher than any serial accepted before for that
  provider. The highest accepted serials are kept in `org/connector_config_serials.json`, which
  survives removal, so an older file can never be replayed.
- **Storage.** An accepted file is stored unchanged at `org/connector_configs/<provider>.txt`,
  mode 0600, and verified again against the pin at every start. A file that no longer verifies
  refuses the start, naming the provider.
- **Admin page.** The Services row shows the source (`Your organization's own app (client
  <client_id>), connector config serial <n>, signed <date> with key <fingerprint>`). It has
  **Upload a connector config** and **Remove our own app**. The second falls back to the built-in
  app, or to none.
  - Both are sensitive (a passkey always, SEC-04), CSRF-checked and audit-logged (`Set the <Label>
    connector config (serial <n>)`, `Removed the <Label> connector config`).
  - Neither the page nor any response or log ever shows the secret.
  - Upload and removal apply live, as `offer_service` does (D6).
- **Precedence in the effective config (amends D4).** In organization mode a provider's section is
  its connector config, otherwise its built-in app, otherwise none. A legacy bundle's credential
  sections are never read after their import.
- **Module.** `src/privacyfence/connector_configs.py`, with `parse`, `verify`, `accept`,
  `load_all`, `remove` and `import_from_legacy_bundle`, at 100 % coverage. It imports
  `org_bundle_signing` for the Ed25519 primitives.

**Upgraded servers: the legacy bundle (amends D13, D14).**
- Adoption (D13) still copies `org_config.json` to `org_bundle.json`, so a downgrade keeps working.
- In organization mode 7.x reads the adopted bundle only for `mode`, `server` and `idp`. A bundle
  value wins over the environment, as D14 says, with SEC-14's rule kept.
- At the first start, `connector_configs.import_from_legacy_bundle` stores each credential section
  as `org/connector_configs/<provider>.json`, with `"source": "legacy-bundle"` and the bundle's
  SHA-256. These are not re-signed: the bundle's verified signature covered them. A later upload
  replaces one.
- Every org-mode start with a bundle present logs at WARNING: `org/org_bundle.json is read only for
  its server and identity-provider settings until PrivacyFence 8. Move them to privacyfence.env
  (privacyfence-app --print-env-from-bundle), then run privacyfence-app --retire-bundle.` The Admin
  page shows the same line.
- **`privacyfence-app --print-env-from-bundle [--idp-secret-file PATH]`** prints the
  `PRIVACYFENCE_*` lines equivalent to the bundle's `mode`, `server` and `idp`. It never prints the
  IdP client secret: with `--idp-secret-file` it writes the secret to that file (mode 0600, refusing
  to overwrite) and prints `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE=<path>`. Without that option it
  prints a placeholder line.
- **`privacyfence-app --retire-bundle`** ends the legacy bundle. It runs only when the
  environment yields the same `mode`, issuer URL and IdP issuer and client ID as the bundle, and
  refuses otherwise, naming the first difference. Then it records `"retired": true` in
  `org/org_bundle_adopted.json` and deletes `org/org_bundle.json`. Adoption (D13) never runs again
  once `retired` is set, so 6.x's untouched `org_config.json` is not copied back. A downgrade to 6.x
  still works, because 6.x reads that untouched file. To re-upgrade from 6.x later, the D13 list
  (which deletes `org_bundle_adopted.json`) applies unchanged.
- An org server installs no new bundle. There is no org-mode bundle upload, and a file at
  `org/org_bundle.json` whose SHA-256 differs from the adopted one is refused at start:
  `Organization servers no longer take a new org_bundle.json; set privacyfence.env and upload
  connector configs on the Admin page.`

**Tooling.**
- **`build_org_bundle.py`** builds desktop bundles only and refuses every organization-mode
  option; D7 lists them and the messages (p10).
- **`scripts/sign_connector_config.py`** (stdlib plus `cryptography`, like `build_org_bundle.py`'s
  signing):
  - `keygen --out KEY.pem` writes a PKCS#8 PEM private key (mode 0600, refusing to overwrite) and
    prints the public key and its fingerprint;
  - `sign --provider P --serial N --client-id ID --key KEY.pem [--login-url ...] [--auth-server-url
    ... --server-url ...] --output FILE`;
  - the client secret is read from `--client-secret-file` or prompted with `getpass`, never from
    the command line, where `ps` and the shell history would show it.
- **The browser tool: `website/tools/connector-config/index.html`**, on privacyfence.eu and as the
  same single file in the repository for offline use.
  - The fields:
    - a provider select, and the provider-specific fields;
    - the serial (prefilled from today's date as `YYYYMMDDnn`);
    - your three text boxes: the client ID, the client secret and the signing key (a pasted
      PKCS#8 PEM);
    - a **Generate a new key** button, which downloads `privacyfence-connector-key.pem` and shows the
      fingerprint.
  - **Sign** downloads `<provider>.pfconnector`.
  - It signs with WebCrypto Ed25519 (Chrome 137 or later, Firefox 129, Safari 17). Other browsers
    get `This browser cannot sign Ed25519; use scripts/sign_connector_config.py.`
  - **It sends nothing anywhere.** The page is self-contained, with no external script, style or
    font. Its CSP meta tag is `default-src 'none'; script-src 'sha256-…'; style-src 'sha256-…';
    connect-src 'none'; form-action 'none'; base-uri 'none'`, and it has no form submission.
  - The page says it runs only in your browser, and that the offline file from the repository is
    the choice when you would rather not trust a web page with your key.
  - It is never served by a PrivacyFence server, because the server is what the signature
    protects.
  - It is linked from the Admin page and the docs, not from the site's navigation.
  - **Release assets.** `scripts/sign_connector_config.py` and the page, as
    `privacyfence-connector-config-tool.html`, are attached to each stable GitHub Release and
    uploaded to the R2 archive, next to `build_org_bundle.py` and `sync_room_directory.py`
    (`build.yml`'s "Publish release artifacts to Cloudflare R2" and "Attach release assets"
    steps). That copy is the offline one, pinned to a release.
- **Cross-language tests.** `tests/fixtures/connector_config/` holds a key, inputs and the expected
  file. The Python tool must reproduce it byte for byte, and the server must verify it. A Node test
  (`tests/unit/test_connector_config_web_tool.py`, which runs `node` on the page's extracted script
  and fails rather than skips when `CI` is set) must produce the same signature. Ed25519 signatures
  are deterministic, so the bytes match.

**Security.**
- A hijacked admin session, even with its passkey, cannot swap in an attacker's app: that needs
  the signing key. It can remove an own app, which falls back to the built-in app and is audited.
- The pin is trust on first use. The first upload on a new server decides the key, so the docs
  tell admins to check the fingerprint shown before confirming.
- The web tool is the soft spot. Whoever controls privacyfence.eu could change the page to steal
  keys. The CSP, the single self-contained file, and the offline copy in the repository are the
  mitigations, and ADR 0268 records the residual risk.

ADR 0268 records this. It amends 0016 and 0200 (organization mode no longer uses a bundle) and
relates to 0255, 0263 and 0262.

### D26. The built-in Salesforce app is a packaged External Client App

This section overrides D1, D19 and mb1 where they say "Connected App" for Salesforce.

**Why packaged.** Since Spring '26, Salesforce no longer lets an org create new Connected Apps;
new integrations use External Client Apps. An External Client App whose distribution state is
**Local** works only inside the org that created it. The built-in app serves users of every
customer's org, so it is **Packaged**: it ships in a second-generation (2GP) managed package, which
each customer org installs once.

**What PrivacyFence owns (mb1).**
- Two Developer Edition orgs, owned by `apps@privacyfence.eu` (Salesforce usernames
  `apps@privacyfence.eu.devhub` and `apps@privacyfence.eu.ns`):
  - the **Dev Hub**, non-ephemeral, which holds the External Client App's global OAuth settings
    (the consumer key and secret);
  - the **namespace org**, which registers the package namespace (`privacyfence`, or `pfence` if
    taken) linked to the Dev Hub.
- **The External Client App.**
  - Name and API name `PrivacyFence` (its metadata members are `PrivacyFence`,
    `PrivacyFence_oauth` for the OAuth settings and `PrivacyFence_oauthPlcy` for the policies),
    distribution state Packaged, contact `info@privacyfence.eu`. The package itself is also named
    `PrivacyFence`.
  - Callback `https://oauth.privacyfence.eu/callback/salesforce`, scopes `api` and
    `refresh_token, offline_access`.
  - The authorization code flow, with PKCE and the client secret both required, and refresh token
    rotation on.
  - Its description, info URL, logo and icon are fixed in mb1. The logo and icon are served from
    privacyfence.eu.
- **The package.** A 2GP managed package `PrivacyFence` contains the app and its OAuth settings,
  without its global OAuth settings and without its OAuth policies: Salesforce does not package
  `ExtlClntAppOauthConfigurablePolicies`, because each installing org sets its own. So every subscriber org shares the one consumer key and secret, and those never leave
  the Dev Hub.
  - Versions are created with `--installation-key-bypass` and promoted, so anyone can install the
    released version.
  - Its source, minus the global OAuth settings, which hold the secret, is committed to
    `salesforce/package/`, so later versions can be built from the repository.
- **Two values come out of mb1:**
  - the consumer key and secret, as `PF_APP_SALESFORCE_CLIENT_ID` and `..._SECRET`, as before;
  - the install URL `https://login.salesforce.com/packaging/installPackage.apexp?p0=<04t version
    id>`, as the repository **variable** (not a secret) `PF_APP_SALESFORCE_INSTALL_URL`.

**The install URL ships with the built-in app (amends D1, p1).**
- `scripts/builtin_apps.py write` also reads `PF_APP_SALESFORCE_INSTALL_URL`, and stores it as the
  `install_url` field of the `salesforce` entry when it is an `https://login.salesforce.com/`
  URL. Any other value is a warning, and the field is left out.
- `build.yml` and `publish-pypi.yml` pass it from `vars.` in the same step env as the secrets.

**What each customer org does once.**
- Its Salesforce admin opens the install URL, installs the package for admins only, then opens
  **Setup → External Client App Manager → PrivacyFence → Policies**:
  - either **Permitted users: All users may self-authorize**;
  - or **Admin approved users are pre-authorized**, plus the permission sets of the people who may
    connect.
- Until that is done, Salesforce refuses the sign-in. PrivacyFence then shows the existing
  `/connect?error=salesforce` page, with the line `Your Salesforce administrator has to install
  PrivacyFence in your org first.` when Salesforce's error says the app is not installed (OAuth
  error `OAUTH_APP_ACCESS_DENIED` or `invalid_client_id`).
- The Admin page's Salesforce row, when it uses the built-in app, says `Each Salesforce org
  installs PrivacyFence once: send its Salesforce admin this link.` with the install URL and a copy
  button.

**Login URL (Admin page).**
- The built-in app signs in at `https://login.salesforce.com`. An org that blocks that host (My
  Domain's **Prevent login from https://login.salesforce.com**) needs its My Domain URL.
- So `services.salesforce` in the admin config gains `login_url` (default `""`, which means
  `https://login.salesforce.com`). A new action, `set_salesforce_login_url(value: str)`, sets it.
  - Both modes, sensitive, applies live.
  - It accepts only `https://login.salesforce.com`, `https://test.salesforce.com`, or an
    `https://<name>.my.salesforce.com` or `https://<name>--<sandbox>.sandbox.my.salesforce.com`
    origin with no path. Otherwise it refuses: `Use login.salesforce.com, test.salesforce.com or
    your My Domain URL (…my.salesforce.com).`
  - Limiting the host to Salesforce's own names keeps an admin from sending people's Salesforce
    sign-in to another site.
- The effective config puts it into the Salesforce section as `login_url`, for the built-in app
  and for a connector config without one. A connector config's own `login_url` wins.

**Own apps are unchanged.** An organization's own Salesforce app is a **Local** External Client App
in its own org. It needs no package, and works for that org's users. On a server it is a connector
config (D25); on a desktop install, a bundle section. `docs/salesforce-setup.md` already describes
it, minus the "Connected App" alternative, which goes.

**Known uncertainty.** A community report describes a subscriber's web server flow, with the
packaging org's consumer key, failing with a cross-org error. Salesforce's Trailhead module says
subscribers can share the original credentials. mb1 ends with a test install in the QA org and a
real sign-in through the relay. If that fails, stop before p1 and decide with the maintainer: ship
7.0 without a built-in Salesforce app (own app only), or follow Salesforce's guidance on
subscriber credentials.

ADR 0254 records this (p12 folds D26 into it).

## ADRs

- **0254** — Built-in PrivacyFence OAuth apps for Google, Slack, Atlassian, Salesforce and Miro
  ship in the server distributions (the org image and the PyPI wheel), baked in from CI secrets by
  `scripts/builtin_apps.py`. They serve organization mode only, always through the relay; desktop
  mode, deprecated in 7, gets none (D19). Relates to 0040: same mechanism, new apps; 0040 gets no
  status line.
- **0255** — `org_config.json` holds only the startup settings (`mode`, `server`, `idp`) and app
  credentials, stays signed, and a provider it does not carry uses the built-in app. Amends 0196
  (which services are offered is no longer read from the bundle) and 0016 (the signature no longer
  covers operational settings). Peek and Grist auto-provisioning becomes an Admin-page switch, and
  the addresses its key is sent to come only from the environment (D18); relates to 0241, 0244.
- **0256** — Operational settings live in a daemon-written `org/admin_config.json`, imported once
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

  Amends 0196 and 0253 (the AI token proposals switch, D21); relates to 0016, 0034, 0068, 0070,
  0200.
- **0257** — Administration is a separate top-level Admin page; Settings shows every person the
  same personal sections. The org Privacy and Stats sections move to Admin. Amends 0217 (where
  Stats lives); relates to 0033.
- **0258** — Org servers use the built-in apps through the PrivacyFence-hosted redirect relay at
  `oauth.privacyfence.eu`. It is deployed from `main` on its own track, never by a release tag,
  and its contract only grows. It forwards only to hosts that proved control of their domain, keeps no
  logs, shows registrations only to maintainers, and maintainers can revoke a host. It runs in the
  existing Cloudflare account, with required reviewers on deploys. It forwards to a loopback
  host without registration. New registrations are emailed to the maintainers through the
  Gmail API with a send-only token, from an account restricted to internal delivery. It is offered as is, under the published relay terms, with no
  warranty and liability limited as far as the law allows (D16). Relates to 0011, 0186.
- **0259** — `mode` and the server address may come from environment variables. Organization mode
  without an IdP is single-user mode, with one administrator, allowed only on `http://localhost` or
  `127.0.0.1`, signed in with a one-time setup code and then a passkey. The Docker image starts in
  organization mode. Relates to 0011, 0177, 0213.
- **0260** — Local (desktop) mode is deprecated in 7.x, announced in the log, status, pages and
  companion, and is to be removed in 8.x. Relates to 0002, 0003.
- **0261** — 7.x is the migration checkpoint.
  - Every start migrates every person's data eagerly, then records `data_format.json`
    (`format: 7`).
  - 7.x refuses data whose format is newer than its own.
  - 8.x may delete the pre-7 converters and refuses data without the marker, telling people to
    install 7.x first.

  Relates to 0047, 0171, 0172, 0228, 0236, 0041.
- **0262** — The 6 → 7 upgrade is reversible. 7.x adopts `org_config.json` and its pin files by
  copying them to new names (`org_bundle.json`, `org_bundle_signing_pubkey.txt`,
  `org_bundle_serial.txt`) and never changes a 6.x file, so the latest 6.x keeps running after a
  downgrade. Amends 0016 and 0200 (the file names); relates to 0261.
- **0263** — An organization server can take its IdP and server address, and the Peek, Grist and
  Grist-provisioner addresses (D18), from environment variables, with no bundle. An upgraded
  server's legacy bundle, the only bundle an organization server reads (0268), must still be signed
  and wins for the IdP. Automatic Peek and Grist
  accounts send their key only to addresses from the environment. Amends 0016 (org mode no longer
  always needs a signed bundle) and 0011; relates to 0241, 0244.
- **0264** — Admins can be named by IdP-verified email (`PRIVACYFENCE_ADMIN_EMAILS` or
  `idp.admin_emails`), alongside the group claim. Relates to 0011.
- **0265** — Live connector checks run on a dedicated runner, `runner.privacyfence.eu`
  (Hetzner, behind the Cloudflare proxy, administered only over Tailscale), labelled
  `privacyfence-live` in its own runner group. They run in organization mode as the principal
  `qa-live` and test the built-in apps, so 8.x needs no runner change; the runner's bundle is
  optional. The host also serves a QA organization server, deployed from `main` with `/mcp`
  blocked at its proxy, on which the QA accounts are connected in a browser; the runner copies
  their tokens from its data directory (D22). A new connector lands as two PRs, connect first,
  then tools with live recording. Amends 0019 (the host) and 0181 (the group and label); relates
  to 0254, 0258.

- **0266** — An organization server may opt in to PrivacyFence's built-in Google sign-in
  (`PRIVACYFENCE_IDP=builtin-google`): a separate `privacyfence-signin` client through the relay,
  default-deny (only admins by verified email and allowed Workspace domains with `hd`), each
  sign-in domain proven by a DNS TXT record and enforced by the relay on Google's `hd`, and never
  the default. Relates to 0262, 0263, 0258.
- **0267** — Pre-releases publish the organization image too, under their exact version tag only;
  `X.Y`, `X` and `latest` move only for stable releases (D23). Amends 0208.
- **0268** — Organization servers read no bundle: startup settings come from the environment, and
  an organization's own app is a signed per-provider connector config uploaded on the Admin page.
  Its format is signed text, its key is pinned on first use, and its serials only increase. It is
  signed with `scripts/sign_connector_config.py` or a self-contained browser tool on
  privacyfence.eu. An upgraded server reads its 6.x bundle's server and IdP settings read-only
  until 8.x; desktop installs keep the bundle (D25). Amends 0016, 0200; relates to 0255, 0262,
  0263.
- **0269** — The built-in Slack app stays the default. While Slack throttles it outside the
  Marketplace, channel history is read through search, and the Admin page offers a one-click
  internal app for full speed (D24). Relates to 0254, 0268.

## Manual steps

Step-by-step page: https://claude.ai/artifact/TumFGa3FRUqYkvuQyuTkGB

- **Before:**
  - (mb1) Register the six PrivacyFence-owned apps (including the Google sign-in client in its own
    project, D20), each with only its relay redirects, and store their 12 values as repository
    secrets.
  - (mb2) Create the Cloudflare token and the relay Worker's GitHub environment.
  - (mb4) Build the new live-check runner, `runner.privacyfence.eu` (D17): Hetzner, Tailscale,
    the Cloudflare record and origin certificate, the `privacyfence-live` runner group and the
    runner itself.
- **After the merge** (D23; nothing is tested on a branch build):
  - (ma0) Approve the relay's first deploy from `main` and run the runbook's smoke tests.
  - (ma6) Install the QA server's deploy files on the runner host, let it deploy from `main`, and
    connect every QA account on its `/connect` page in a browser.
  - (ma7) Run the live check on `main` on the new runner, then retire the old runner and wipe its QA
    state.
  - (ma10) Cut `7.0.0a1` with `/cut-release`, and check that GHCR has the `7.0.0a1` image and that
    `latest` did not move.
  - (ma2) Start a fresh organization server from the alpha image the way the Docker guide says,
    and connect through the relay. Try the built-in Google sign-in.
  - (ma3) Upgrade a 6.x org server with an old-style bundle to the alpha image, use the Admin page,
    compare Settings for an admin and a normal user, and go back to 6.x.
  - (ma4) Run the alpha image in single-user mode with `docker run`: setup code, passkey, a
    connector through the relay, and an MCP client signing in.
  - (ma1) Install the desktop alpha from the download page: an own-app bundle still connects as in
    6.x, no built-in app is offered, and the deprecation notices show.
  - (ma8) Record the demo video on the alpha and submit the built-in Google app for verification,
    with the scope justifications from the guide. Also submit the `privacyfence-signin` project
    for brand verification (D20).
  - (ma11) Once every check above passes, cut 7.0.0.
  - (ma9) Submit the built-in Slack app to the Slack Marketplace (D19), once Slack's requirements
    are met.
  - (ma5) After tagging 7.0.0, run the follow-up website session from the guide; have the relay
    terms reviewed by a lawyer.

## Risks and open questions

- **The built-in Google sign-in is a single point of failure for servers that choose it.** D20
  keeps it opt-in, in a separate Google project, and documents an organization's own IdP app as
  the robust choice.

- **The built-in Slack app is throttled until it is in the Slack Marketplace, which needs 10
  active workspace installs first.** Channel history goes through search, which misses some
  message types and lags slightly, and thread replies stay at 15 per call and 1 call a minute
  (D24). The Admin page's one-click internal app removes the limit for one workspace.
- **Connector configs trust the first key uploaded (D25).** Whoever uploads first on a new server
  pins the key, so the docs tell admins to check the fingerprint. The browser signing tool is only
  as trustworthy as privacyfence.eu; the offline copy in the repository is the alternative.
- **Organization-mode bundles are refused from 7.0 (D25).** An operator's script that builds or
  installs a new org bundle stops working; the refusal names `privacyfence.env` and connector
  configs, and `--print-env-from-bundle` writes the environment from an existing bundle.
- **Desktop users get no built-in apps.** That is deliberate: desktop mode ends in 8 (D19). The
  deprecation notice says the built-in sign-in is one more reason to move.

- **Automatic accounts are unit-tested only.** No manual step runs a real Peek or Grist
  provisioner. The composed section goes through main's unchanged `from_org_config` validation,
  and main's provisioning tests cover the workers.
- **6.11's auto-provisioning bundle flags are refused in 7.0.** Upgraded servers keep working,
  because their addresses are imported locked (D18). But an operator's script that rebuilds the
  bundle with `--peek-auto-provision` gets the refusal, which points to the Admin page and the
  environment variables. The CHANGELOG lists it under "Changed" as breaking.
- **The built-in Google app is unverified when 7.0 ships.** Google verification needs the demo
  video and the live Limited Use privacy section, so it starts only after the merge (ma8).
  - Until Google approves it, people see an "unverified app" screen, and the app is capped at 100
    users in total across every PrivacyFence install.
  - The restricted Gmail and Drive scopes also need a yearly third-party security assessment
    (CASA), which Google schedules during review.
  - Mitigation: the 7.0 release notes say this. They point organizations to their own Google app
    (an Internal app has no cap). The maintainer may hold the 7.0 tag until Google approves.
- **Google may ask for narrower Calendar scopes.** The justification argues for `calendar` over
  `calendar.events` plus `calendar.readonly`. If the reviewer insists, the fallback is a code
  change in a follow-up, and everyone reconnects Calendar.

- **The relay terms are not legal advice.** The plan writes them from the decisions above (D16), and
  ma5 asks for a lawyer's review. Until then they rest on the publisher's own judgement.
- **The live-check cutover is a single switch.** `main`'s workflows move to the
  `privacyfence-live` label when the PR merges. Until ma6 has connected the QA accounts on the new
  QA server, the live check fails at its token copy. The fix is to finish ma6, not to point the
  workflows back at the old runner.
- **Every manual check runs after the merge, on an alpha (D23).** The PR merges on CI and review
  alone, so a defect the manual steps find is fixed forward on `main` before the next alpha.
- **Open question: 6.x fixes during the alpha.** `main` is 7.0 from the merge on, and releases are
  tagged only from `main`'s tip. If a 6.x bugfix release is needed before 7.0.0, the repository
  needs a 6.x maintenance branch and a release path for it, which it does not have today. Keep the
  alpha phase short.
- **The QA Peek is public** at `peek-qa.privacyfence.eu` (D22), because its device sign-in runs in
  the maintainer's browser. It holds only QA accounts and seed data, and Peek's own sign-in
  protects it.
- **The QA server and the runner share one directory** (D22). Each can change what the other
  reads; both hold only QA accounts, and `/mcp` is closed so the server never uses the tokens.

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
  container's environment, which is the same party that picks the image. ADR 0263 states this. A
  bundle stays available for operators who want a signed configuration.
- **Admins by email depend on `email_verified`.** An IdP that does not send it cannot use the email
  list, and its admins use the group claim instead.

- **Downgrade target is the latest 6.x.** D12 rewrites rules into formats 6.10.x reads: version-3
  rows, canonical verbs, tokens with their own rules. 6.9.x and older cannot read tokens with
  their own rules. The docs name the latest 6.x (6.10.0 or newer) as the release to go back to.
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
  needs the setup code from the container log. ADR 0259 and the Docker docs say this plainly.
- **Single-user sign-in is new authentication code.** p16 runs on Opus for that reason (see its
  `worker_model_reason`).

- **Fresh org servers start with built-in services off.** Relay registration starts only once an
  admin offers a built-in service. Single-user mode starts with them on; a desktop install has
  none (D19).
- **A bundle that adds a provider later** does not offer it by itself after the import. An admin
  switches it on, and the docs say so.
- **Desktop installs lose bundle enforcement of the offered services.** An IT-distributed bundle
  used to decide which services a desktop install offers. After the import, the install owner
  decides on the Admin page. This follows from moving the switches out of the signed bundle, and
  ADR 0256 records it.
- **Signing no longer covers the operational settings.** `authz`, `step_up`, `audit_forwarding`
  and `scoped_tokens` were covered by the bundle signature and now are not. They have the same
  protection as the install-wide settings.yaml. ADR 0255 and 0256 record this, and step-up can
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
  - Every Salesforce org's admin installs the PrivacyFence package once before its users can connect (D26).
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
  title: Register the six PrivacyFence-owned OAuth apps (Google Web client, Slack, Salesforce, Atlassian, Miro, and the Google sign-in client in its own project), each with only its oauth.privacyfence.eu redirects, distribute them, and store their credentials as repository secrets
  why: p1 bakes them into the org image and the PyPI wheel (and its dispatched build.yml check looks for them); ma2 and ma4 sign in with them
  done_when: Settings → Secrets and variables → Actions lists the 12 repository secrets PF_APP_GOOGLE_WEB_CLIENT_ID/_SECRET, PF_APP_SLACK_CLIENT_ID/_SECRET, PF_APP_SALESFORCE_CLIENT_ID/_SECRET, PF_APP_ATLASSIAN_RELAY_CLIENT_ID/_SECRET, PF_APP_MIRO_CLIENT_ID/_SECRET, PF_APP_GOOGLE_SIGNIN_CLIENT_ID/_SECRET, the Slack, Salesforce, Atlassian and Miro apps belong to apps@privacyfence.eu (an alias of andras@tkcs.name); the Google projects show info@privacyfence.eu as support and developer contact; the Salesforce app is a Packaged External Client App in a promoted 2GP managed package, its install URL is the repository variable PF_APP_SALESFORCE_INSTALL_URL, its source is committed to salesforce/package/ on the plan branch, and a test install in the QA org signed in through the relay (D26); and the Slack, Atlassian and Miro apps are set to public distribution
- id: mb2-relay-worker-cloudflare
  title: Create the Cloudflare API token, the relay's Gmail sender (alias, delivery restriction, Internal OAuth client and send-only refresh token) and the oauth-relay-worker GitHub environment
  why: the relay's deploy workflow (p11) deploys from main with them when the PR merges (ma0), before the alpha that ma2 and ma4 test
  done_when: the repository has an environment named oauth-relay-worker, restricted to main, holding secrets CF_OAUTH_RELAY_WORKER_API_TOKEN, CF_OAUTH_RELAY_WORKER_ACCOUNT_ID, CF_OAUTH_RELAY_REVOKE_TOKEN, CF_OAUTH_RELAY_GMAIL_CLIENT_ID, CF_OAUTH_RELAY_GMAIL_CLIENT_SECRET and CF_OAUTH_RELAY_GMAIL_REFRESH_TOKEN; the API token verifies with the curl in the page; and test@privacyfence.eu has the relay-notify@ alias in its own organizational unit with an outbound-only content compliance rule (not Restrict delivery, which also blocks incoming mail)
- id: mb4-live-runner-host
  title: 'Build runner.privacyfence.eu: the Hetzner server, Tailscale, the Cloudflare DNS records (runner and peek-qa), origin certificate, WAF rule blocking /mcp and firewall, the pfrunner user (uid 10001), and the GitHub runner registered in the new privacyfence-live group; place qa_environment.yaml'
  why: at merge p21 switches the live-check workflows and p22 adds the QA server deploy, all on the privacyfence-live label; without a registered runner in that group they would queue forever
  done_when: 'the organization''s Actions runner list shows runner-privacyfence-eu online in group privacyfence-live (Selected workflows: connector-live-check.yml and qa-record-fixture.yml on main; deploy-qa-server.yml does not exist on main yet and is added in ma6), tailscale ssh reaches the host, ssh to its public address times out, id pfrunner shows uid 10001, and https://runner.privacyfence.eu answers through Cloudflare'
manual_after:
- id: ma0-relay-first-deploy
  title: After the merge, approve the deploy-oauth-relay-worker run on main (it sets the Worker's revoke and Gmail secrets), check the dashboard, and run the runbook's smoke tests
  why: 'D23: the relay deploys only from main, so its first deploy is the merge; the alpha (ma10) must find it live'
- id: ma6-live-runner-state-and-qa-server
  title: After the merge, install deploy/qa-runner/ on the runner host (deploy script, sudoers rule, compose override, Caddyfile, env file, IdP secret), add deploy-qa-server.yml@refs/heads/main to the privacyfence-live runner group's selected workflows, let deploy-qa-server.yml deploy main, then sign in as test@privacyfence.eu and connect every service on /connect in a browser (Google, Slack, Salesforce, Atlassian, Miro, Telegram, Grist, Peek)
  why: 'D22: the runner copies its tokens from the QA server, so they exist only once someone connects them there; the deploy files come from main'
- id: ma7-live-runner-cutover
  title: 'After ma6, dispatch connector-live-check on main, confirm it ran on runner-privacyfence-eu with every connector green and relay: up, then decommission the old privacyfence-test runner, its group and its QA state'
  why: the cutover happens only when main's workflows carry the new label; the old host holds live tokens until it is wiped
- id: ma10-cut-alpha
  title: Cut 7.0.0a1 with /cut-release, and check that GHCR has ghcr.io/privacyfence/privacyfence-org:7.0.0a1 with its attestation, that latest and the floating tags did not move, and that the download page offers the desktop pre-release
  why: 'D23: every remaining check installs this alpha the regular way'
- id: ma2-org-relay-signin
  title: On a fresh test host, start an organization server from the 7.0.0a1 image exactly as docs/org-mode-docker.md's quick start says (env file, no bundle), offer the built-in services, register with the relay and connect Gmail, Slack, Salesforce, Jira and Miro; test revoke and restore; try the built-in Google sign-in; switch Slack to the workspace's own app with the Admin page's one-click manifest, sign it with the browser tool and upload the connector config
  why: the relay round trip, the env-only start and the built-in sign-in need a public domain, the real Worker and real providers, installed the way an operator installs
- id: ma3-upgrade-and-admin-page
  title: Upgrade a 6.x org server with an old-style bundle to the 7.0.0a1 image (PRIVACYFENCE_IMAGE_TAG, docker compose pull, up -d), check the one-time import, use the Admin page, compare Settings for an admin and a normal user, and go back to the 6.x tag; check that the bundle's credentials became connector configs, and move its server and IdP settings to privacyfence.env with --print-env-from-bundle
  why: checks the migration, the admin path with step-up, and the downgrade end to end on a regular Docker install
- id: ma4-docker-single-user-and-deprecation
  title: Run the 7.0.0a1 image in single-user mode with the docs' docker run command (setup code, passkey, a connector through the relay, an MCP client)
  why: single-user mode's browser ceremonies, the real relay and a real MCP client cannot run in CI
- id: ma1-desktop-unchanged
  title: Install the 7.0.0a1 desktop pre-release from the download page, check that without a bundle no built-in app is offered, connect Gmail and Slack with an own-app bundle as in 6.x, and check the deprecation notices (log, page strip, companion, privacyfence_status)
  why: D19 keeps desktop mode on own apps only; a real install with real consent screens is the only check that nothing regressed
- id: ma8-google-verification
  title: Record the demo video on the alpha (single-user and org server sign-ins through the relay, every scope in use), then submit the privacyfence-builtin Google app for verification with the scope justifications in the guide, and the privacyfence-signin project for brand verification
  why: Google requires a video of the real consent flow with the built-in client IDs and every scope in use, plus a live privacy policy with the Limited Use disclosure (p13c, deployed on merge). Until verified, the built-in Google app shows an unverified-app screen and is capped at 100 users
- id: ma11-cut-stable
  title: When ma0 to ma8 pass on the latest alpha (ma8 submitted, not necessarily approved), cut 7.0.0 with /cut-release and check that latest moved to it
  why: 'D23: the stable release follows the alpha checks; fixes found on the alpha land as normal PRs and a new alpha first'
- id: ma9-slack-marketplace
  title: After 7.0.0, submit the built-in Slack app to the Slack Marketplace with the listing texts in the guide, once Slack's submission requirements are met; after approval, flip oauth_apps.SLACK_MARKETPLACE_LISTED in a one-line PR
  why: a distributed Slack app outside the Marketplace is limited to 1 request per minute on channel history and thread reads (D19); the listing needs a released app and Slack's review
- id: ma5-post-release-website-and-legal-review
  title: After tagging 7.0.0, run the follow-up website session from the guide (product and connector pages for 7.x, the Docker single-user download path, links to /docs/oauth-relay/), and have the relay terms reviewed by a lawyer
  why: 'ADR 0099: product pages land only after the stable release that documents them, and the relay terms are a legal text the plan cannot vouch for'
verify_after_merge:
- python3 -m mypy src/privacyfence
- python3 scripts/mypy_strict_modules.py
- test ! -d cloudflare/oauth-relay || (cd cloudflare/oauth-relay && npm ci && npm run typecheck && npm test)
final_checks:
- docs/builtin-oauth-apps-plan.md and docs/builtin-oauth-apps-plan-manual-steps.html are deleted, and grep -rn "builtin-oauth-apps-plan" --exclude-dir=.git . finds nothing
- ADRs 0254-0269 (or their renumbered successors) exist, are Accepted, and are listed in docs/adr/README.md's index; ADRs 0011, 0016, 0019, 0181, 0196, 0200, 0208, 0217 and 0253 carry new Status lines; ADR 0040 is unchanged
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
       - Do the same for every user-facing message D13 lists: org_mode.py (:51-654, including the auto-provisioning messages) and step_up_config.py (:293, :299) say org_bundle.json. Update the tests that pin those texts. Leave web/mcp_dispatch.py and the sync runner to p18.
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
  - .github/workflows/build.yml
  - .github/workflows/publish-pypi.yml
  brief: |-
    Read the plan's "Design → D1" section first; every name, env var, message and module format is there.
    1. Create scripts/builtin_apps.py by copying the structure of scripts/telegram_credentials.py (stdlib only, REPO_ROOT, render(), write(creds_path, environ), archive_members/contains_credentials/check_dist, argparse main with `write` and `check-dist [--require] files*`). KEYS = ("google_web", "slack", "salesforce", "atlassian_relay", "miro", "google_signin"); env vars PF_APP_<KEY.upper()>_CLIENT_ID / _CLIENT_SECRET, plus PF_APP_SALESFORCE_INSTALL_URL for salesforce's optional install_url (D26: only an https://login.salesforce.com/ URL, otherwise a warning and no field). render(apps) returns exactly "APPS = " + repr(dict(sorted(apps.items()))) + "\n". Messages exactly as D1 says (check-dist's text is telegram_credentials.py:94-99's with "Telegram" replaced by "built-in OAuth apps" and ADR 0040 by ADR 0254); ASCII only.
    2. Add `src/privacyfence/_builtin_apps.py` to .gitignore directly below the `_telegram_credentials.py` line, and `include src/privacyfence/_builtin_apps.py` to MANIFEST.in directly below line 6, extending the comment above it to name both generated modules.
    3. In src/privacyfence/app_credentials.py add BUILTIN_APP_KEYS and builtin_app(key) per D1 (a literal `from . import _builtin_apps` with the same `# type: ignore[attr-defined]` comment the Telegram import carries; the PRIVACYFENCE_BUILTIN_APPS JSON-file fallback; one logger.warning naming only the env var on a bad file). Extend the module docstring by one paragraph naming ADR 0254.
    4. Do not call the generator from the desktop installers (scripts/build_dmg.sh, build_deb.sh, build_installer.ps1): desktop mode gets no built-in apps (D19).
    5. .github/workflows/build.yml: in `build-org-image` only, add a step "Write built-in app credentials" right after "Write Telegram app credentials" (same shape, the 12 `PF_APP_*: ${{ secrets.PF_APP_* }}` entries plus `PF_APP_SALESFORCE_INSTALL_URL: ${{ vars.PF_APP_SALESFORCE_INSTALL_URL }}` in that step's env only, `python3 scripts/builtin_apps.py write`) and a step "Check built-in app credentials are packaged" right after the Telegram check step (`python3 scripts/builtin_apps.py check-dist "${REQUIRE[@]}" dist/*.whl`, where REQUIRE is --require on every release tag, stable or pre-release, because the alpha image is how 7.0 is tested (D23); only an untagged workflow_dispatch run warns). The desktop build jobs get no PF_APP_ entry.
    6. .github/workflows/publish-pypi.yml `build` job: the same two steps next to the Telegram ones, with check-dist over `dist/*.whl dist/*.tar.gz` and the same every-tag REQUIRE rule. The secrets go only to the write step's env, never to the job.
    7. Tests. tests/unit/test_builtin_apps_script.py mirrors every test in tests/unit/test_telegram_credentials.py: it generates an importable module whose APPS equals the input; a value with quotes and backslashes stays inert (ast.literal_eval of the right-hand side equals the input); one-of-two set warns and skips; none set deletes a stale module; a newline in a value returns 2 and writes nothing; output is ASCII; check-dist pass, require-fail, warn, no files and unknown archive; main dispatch; git-ignored but packaged; build.yml's build-org-image job and publish-pypi.yml call `scripts/builtin_apps.py write`, while build_dmg.sh, build_deb.sh, build_installer.ps1 and build.yml's desktop jobs contain neither `builtin_apps.py` nor `PF_APP_`, build.yml and publish-pypi.yml call `scripts/builtin_apps.py check-dist`, and none contains `APPS = `. tests/unit/test_app_credentials.py: builtin_app returns the generated module's entry (monkeypatch sys.modules["privacyfence._builtin_apps"]), returns a copy, falls back to the JSON file, returns None and warns once for a missing or malformed file without logging its content; and BUILTIN_APP_KEYS order.
    8. Run ruff check . and python3 -m pytest tests/unit/test_builtin_apps_script.py tests/unit/test_app_credentials.py tests/unit/test_telegram_credentials.py tests/unit/test_org_image_workflows.py -q.
    9. After pushing the phase branch, dispatch build.yml on it (it is in ci.dispatchable; mb1's secrets are in place by now). In the run, the build-org-image "Write built-in app credentials" step's log must show `built-in app <key>: included` for all six keys. If they show `not set`, check the env wiring of step 5; if the secrets themselves are missing, stop with status=blocked naming mb1.
    Stop condition: if `git grep -n "_telegram_credentials" setup.py` shows that setup.py handles the Telegram module, mirror it for _builtin_apps; if the Telegram module reaches the wheel by a mechanism you cannot mirror, stop with status=blocked.

    Also apply D15 SEC-13 / D19: the desktop installers ship no built-in app module; the test in step 7 pins it.
  acceptance:
  - python3 -m pytest tests/unit/test_builtin_apps_script.py tests/unit/test_app_credentials.py tests/unit/test_telegram_credentials.py tests/unit/test_org_image_workflows.py -q passes
  - grep -c "scripts/builtin_apps.py write" .github/workflows/build.yml .github/workflows/publish-pypi.yml reports at least 1 for each file
  - grep -n "builtin_apps.py" scripts/build_dmg.sh scripts/build_deb.sh scripts/build_installer.ps1 finds nothing
  - grep -n "src/privacyfence/_builtin_apps.py" .gitignore MANIFEST.in matches in both files
  - 'the dispatched build.yml run''s build-org-image "Write built-in app credentials" log contains "built-in app google_web: included"'
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
         - an empty bundle in org single-user mode offers google, slack, salesforce, atlassian, miro and telegram; in local mode it offers only telegram (D19);
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
    Also apply D18 (admin config): services.peek and services.grist gain auto_provision {enabled, trust_unverified_email, key_env} with the default key_env names; "peek" stays in LEGACY_BUNDLE_KEYS and grist.auto_provision joins the legacy sub-keys; import_from_bundle copies enabled, trust_unverified_email and a non-default key_env, imports the addresses like any legacy address; when the bundle had auto_provision enabled (a 6.11 server), it imports base_url/server_url as locked addresses (address_locked: true) and provisioner_url into services.grist.auto_provision.provisioner_url, so automatic accounts keep working after the upgrade; the SEC-11 re-import never changes auto_provision.enabled. Test each rule.
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
       - Parametrized over google, slack, salesforce and miro × local and org × (bundle credentials, built-in present, built-in absent): the exact section, with relay True for every built-in section; in local mode a present built-in gives no section and credentials_source "none" (D19); google carries auth_uri/token_uri; salesforce built-in uses consumer_key/consumer_secret.
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
    Also apply D18 (environment addresses and effective config): add oauth_apps.environment_addresses() reading PRIVACYFENCE_PEEK_BASE_URL, PRIVACYFENCE_GRIST_SERVER_URL and PRIVACYFENCE_GRIST_PROVISIONER_URL (normalised; a bad value raises ConfigurationError naming the variable); environment addresses win over admin ones; auto_provision is added to the peek or grist section only under D18's conditions (admin enabled, org mode, not single-user, addresses from the environment or imported locked addresses, no Grist OAuth app). Tests: each condition missing leaves no auto_provision; the composed section passes org_mode.PeekProvisioningConfig/GristProvisioningConfig.from_org_config with enabled=True; an admin address never enables provisioning.
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
       - daemon_main.py:560, which checks the signature field on the raw bundle;
       - daemon_main.py:1122, a docstring;
       - settings_controller.py:1295, 1385 and 1745;
       - routes_connect.py:141;
       - org_mode.py:672.
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
         - With an org-mode bundle that has no slack section and a built-in slack app, build_connectors(config, org_config) reaches the Slack branch; in local mode it does not (D19).
         - The CLI form never uses a built-in app, even when the bundle says mode org.
         - _start_org_web_server stores bundle and startup_admin on OrgAuth.
       - test_settings_controller.py:
         - The existing ORG_CONFIG_ENABLING_EVERY_CONNECTOR tests keep passing unchanged, because config_for imports from that bundle.
         - With no bundle and a built-in miro app, the local snapshot does not list Miro (desktop mode has no built-in apps, D19).
         - With miro withdrawn in admin_config.json, it is refused.
         - A bundle upload carrying "authz" over an imported config installs and sets the "are ignored" message.
       - Do not weaken any assertion.
    5. Run ruff check . and python3 -m pytest tests/unit -q.
    Also apply D18 (startup and release commands): the provisioning validation (daemon_main.py ~:1163-1167) and peek_provisioning.start / grist_provisioning.start (~:1221-1222) receive the effective config; run_peek_release and run_grist_release (~:2386, ~:2436) build the org effective config as D18 says instead of reading load_org_config() directly. Test that a withdrawn Peek starts no provisioner and that run_peek_release works with the switch in the admin config and the address in the environment.
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
    6. daemon_main.py _start_org_web_server: after server.start() (line ~1261), call relay_client.start_registration_loop(server_config.issuer_url, org_config, _shutdown_event), then relay_client.request_check().
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
    Also apply D15 SEC-03's relay_client part (too_soon keeps last_ok_at; persist it in org/relay_status.json and load it at startup), SEC-25's relay_client part (too_many_new_hosts is handled like too_soon), SEC-26 (the ERROR line when a token is replaced after a recorded success), SEC-09's well-known rule (404 unless needed), and SEC-19's pending client_id check, each with tests.
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
    Also apply D21: set_scoped_tokens takes allow_ai_proposals: bool and stores it in scoped_tokens.allow_ai_proposals; test that it is stored, that policy_from_org_config over the result still needs enabled, and that an imported bundle's allow_ai_proposals survives the import.
    Also apply D19: define oauth_apps.SLACK_MARKETPLACE_LISTED = False in oauth_apps.py (D24's limited mode and Admin line, built in p24, read it). Also apply D18 (Admin actions and rows): add set_auto_provisioning (org only, refused with D18's texts when unavailable); set_server_address is refused while the address comes from the environment or is an imported locked address (D18); service_rows gains options.address_source, options.locked and options.auto_provision {enabled, trust_unverified_email, available, reason, key_set} for peek and grist (key_set from org_mode.provisioning_key once p20c adds it; until then os.environ.get(key_env) is acceptable and p20c switches it). Never put the key in a row. Test each.
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
    Also apply D18: set_auto_provisioning is in the org action table (sensitive, always a passkey per SEC-04, applies on restart) and not_applicable in single-user mode.
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
    Also apply D16: the relay status line ends with a "Relay terms" link to https://privacyfence.eu/relay-terms/, with a test.
    Also apply D18 (Admin rows): renderServices shows D18's server and automatic-accounts lines, the on/off switch and the trust checkbox for peek and grist when available, the unavailable reason otherwise, and no address form when locked; test each state.
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
         - the Grist and Peek address forms (set_server_address), with no Grist or Peek form when options.locked;
         - the unattended-sessions toggle;
         - the room directory: count and synced date, a file input dispatching the intercepted upload_rooms action, and a Clear control (clear_rooms).
         For the forms, copy the structure of the Grist API-key modal at settings_window_html.py:1970-2020.
       - Move the Organization Configuration card (renderGeneral, :705-712) from the local settings surface to the admin surface's org_config section. Change its description to "Your own app credentials, and the server and identity-provider settings, from your IT administrator." It keeps dispatching install_org_config, and shows state.error when set.
    2. routes_settings.py: now that no settings surface dispatches install_org_config, remove /api/settings/org_config/upload and its _BESPOKE_SENSITIVE_ROUTE_PATHS entry. Move the remaining settings-path upload tests in test_routes_settings.py to /api/admin/org_config/upload (in test_routes_settings.py or test_routes_admin.py), keeping their assertions.
    3. tests/unit/test_settings_window_html.py:
       - The features section renders each form prefilled from a sample state.
       - A locked grist or peek (D18) shows no form.
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
       - tokens also carries D21's allow_ai_proposals checkbox and its "Needs scoped tokens turned on." hint.
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
  title: build_org_bundle.py builds desktop bundles only; the room sync writes a file to upload
  depends_on:
  - p0-bundle-file-rename
  complexity: M
  touches:
  - scripts/build_org_bundle.py
  - tests/unit/test_build_org_bundle.py
  - scripts/sync_room_directory.py
  - tests/unit/test_sync_room_directory.py
  - tests/unit/test_docs_configuration_reference.py
  - tests/unit/test_org_mode.py
  - deploy/docker/privacyfence.env.example
  brief: |-
    Read "Design → D2" (what a bundle still holds, and in which mode) and "D7" (every refused option and message, exactly). D25 says why organization servers take no bundle; this phase only changes the tools.
    1. scripts/build_org_bundle.py:
       - Hide every option D7 lists, both kinds, with help=argparse.SUPPRESS. After parsing, print D7's exact error line for each one given, in the order given, and exit 2 without writing. Find the organization-mode options by destination prefix (server_, idp_) plus --mode org, so an --idp-* option added later is refused too.
       - --mode local stays and is the default.
       - --merge refuses an organization bundle and drops legacy operational keys, with D7's messages.
       - Update the "nothing to write" check (:1012-1020), --mode local's popping (:819-829), the module docstring and the --help epilog per D7.
       - Keep the signing code byte-identical (tests/unit/test_build_org_bundle.py cross-checks it).
    2. tests/unit/test_build_org_bundle.py:
       - One parametrized test, test_moved_flag_is_refused, over every operational option D7 lists, D18's and D21's included, with the exact message and exit code 2.
       - One parametrized test, test_org_mode_option_is_refused, over --mode org and every server_/idp_ option, with D7's exact message and exit code 2, and a check that an option list built from the parser's server_/idp_ destinations matches the parametrization.
       - Pass a valid minimal desktop argument set in each case, so the refusal is what fails; nothing is written.
       - A --merge of a desktop bundle with authz and peek drops them and prints the note; a --merge of a bundle with "mode": "org" or an idp section is refused.
       - A credentials-only desktop bundle still builds and signs as before; every signing and cross-check test still passes.
       - Turn main's tests for the D18 and D21 options (test_enable_and_disable_scoped_token_proposals_flags, test_merge_keeps_allow_ai_proposals and the auto-provisioning builder tests) into refusal and merge-drops tests.
       - The bind-host pin (test_org_mode.py:98-99's comment and the test it names): move it to deploy/docker/privacyfence.env.example's commented PRIVACYFENCE_BIND_HOST default against ServerConfig's, per D7. If that example file does not exist yet (p20a creates it), keep the pin as a test of ServerConfig's default alone and say so in the commit message.
    3. tests/unit/test_docs_configuration_reference.py: _bundle_options() skips actions whose help is argparse.SUPPRESS, per D7. Run it: docs/configuration-reference.md still lists the refused options at this point (p13a rewrites it), and the test only requires listed options to exist. If it also fails on documented options that are now hidden, stop with status=blocked.
    4. scripts/sync_room_directory.py: --output replaces --org-config per D7. Remove the bundle-merge and signature handling, refuse --org-config with D7's message, write the rooms file with mode 0600, and update the module docstring and example.
    5. tests/unit/test_sync_room_directory.py: rewrite the merge and signing tests into output-file tests (the written file's shape, mode 0600, the --org-config refusal). Keep the directory-fetch tests unchanged.
    6. Run ruff check . and python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/test_sync_room_directory.py tests/unit/test_docs_configuration_reference.py tests/unit/test_org_mode.py -q.
    Stop condition: tests/integration/test_org_docker_image.py writes its own bundle dict and does not call the script; if any test or workflow outside the touched files runs build_org_bundle.py with --mode org or a server_/idp_ option, stop with status=blocked and name it.
  acceptance:
  - python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/test_sync_room_directory.py tests/unit/test_docs_configuration_reference.py tests/unit/test_org_mode.py -q passes
  - python3 -m pytest tests/unit/test_build_org_bundle.py -q -k test_moved_flag_is_refused passes and selects at least 40 tests
  - python3 -m pytest tests/unit/test_build_org_bundle.py -q -k test_org_mode_option_is_refused passes and selects at least 10 tests
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
       - A repeated failed register for the same host from the same IP within 60 s → 429 too_soon, and a valid proof from another IP right after still succeeds (SEC-03).
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

    Then the release and operations side, per D5's "How the relay is released": write docs/oauth-relay.md (the maintainer runbook with every item D5 lists, including the exact smoke-test curl commands, and links to https://privacyfence.eu/relay-terms/ and https://privacyfence.eu/privacy/#relay), add it to scripts/build_site.py's CONTRIBUTOR_DOCS and to the contributor half of docs/README.md, add the relay rows to docs/downloads-and-release-kpi.md's Cloudflare resources and credentials tables, and add the health pre-flight line to docs/releasing.md and .claude/commands/cut-release.md. Run python3 -m pytest tests/unit/test_build_site.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q.
    Also implement D9's "The relay on localhost" rules: GET /callback/:grant forwards a loopback host (localhost or 127.0.0.1, optional port) to http://<host>/oauth/callback/<grant>?<query> without the registry, and POST /register still answers 400 host_not_public for it. Test both.
    Also apply D15: SEC-02 (the PSL private-section refusal, generated psl_private.ts from a pinned commit by scripts/update-psl.mjs, tests for x.github.io, a.pages.dev and a.workers.dev; the runbook's review step and revocation turnaround), SEC-03 (per-(host, IP) and per-IP limits, failures never block a later valid proof, an attempts table pruned hourly, no registrations row for unverified hosts), SEC-09 (/registrations needs the maintainer token, 401 otherwise), SEC-10 (the strict parsing rules and every listed negative test), SEC-24 (a callback needs exactly one code or one error), SEC-25 (at most 5 new hosts per address per UTC day, too_many_new_hosts, "notified" on /registrations), SEC-15 (runbook Logpush/WAF/tail checks), SEC-23 (Gmail API sending in src/notify.ts with fetch only: the NOTIFY_FROM var, the NOTIFY_TO constant, the GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET/GMAIL_REFRESH_TOKEN Worker secrets set by the deploy workflow from the CF_OAUTH_RELAY_GMAIL_* environment secrets, the cached access token, the daily cap, notify_failures/notify_last_error/notify_skipped on /registrations, and every listed test; no [[send_email]] binding; the runbook covers minting and rotating the refresh token and the outbound-only content compliance rule), SEC-05 and SEC-13 (runbook: required reviewers, one-account note, the built-in secret rotation section). Each rule has a test in the Worker suite. Also apply D16 "The relay links its terms": GET and HEAD / and /terms answer 302 to https://privacyfence.eu/relay-terms/, the 400 texts end with the Terms sentence, and a successful /register response carries "terms"; test each. Also apply D17 "The relay gains a restore route": POST /admin/restore with its tests and the runbook entry. Also apply D20 "Through the relay": the idp, idp-login and stepup grants with their fixed forward paths (/oauth/idp/callback, /oauth/idp/login-callback, /oauth/stepup/callback), the same registration and parameter rules, and a loopback host refused for them; test each. Also apply D20 "Domain proof for sign-in": signin_domains in /register (at most 20, syntax and public-suffix checks), the DNS-over-HTTPS TXT check at _privacyfence-relay.<domain> against pf-relay=<sha256 of the token>, the signin_domains column, the signin_domains_verified/missing response fields, the hd check on idp, idp-login and stepup with its 403 text, the SIGNIN_REQUIRE_HD variable, and the email lines; stub fetch for DNS and test a verified domain, a missing record, a wrong value, a public suffix, a missing hd, a foreign hd, and the switch off.
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
  - p10-bundle-tooling
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
       - Apply D9's "Types" bullet: OrgAuth.idp and OrgOAuthProvider's idp become Optional, authorize is guarded, routes_org_identity and routes_org_stepup are mounted only when idp is not None (web/server.py:1349, :1373), and the issuer log line (daemon_main.py:1265-1267) handles None.
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
    Also apply D18 (single-user mode): a single-user sign-in calls neither peek_provisioning.notify_sign_in nor grist_provisioning.notify_sign_in, and the effective config never carries auto_provision; test both.
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
    1. web/mcp_dispatch.py (~:772-775 on 1b0985dd, begin_unattended_session's refusal) becomes: "Unattended sessions are disabled. An administrator turns them on on the Admin page (Features) before this connection can be marked unattended."
    2. web/routes_settings.py _SCOPED_TOKENS_DISABLED (~:1522), and the identical text in scoped_tokens.py's _prepare (~:609-612), become: "Scoped tokens are not enabled on this install. Your administrator turns them on on the Admin page." scoped_tokens.PROPOSALS_DISABLED_MESSAGE (~:167-170, D21) becomes: "Token proposals are not enabled on this install. Your administrator turns them on on the Admin page (Scoped tokens)."
    3. settings_controller.py:1299 becomes: f"{connector_label(connector)} isn't offered on this install. Turn it on on the Admin page (Services)."
    4. sync/src/privacyfence_sync/runner.py _UNATTENDED_DISABLED (:39-42) becomes: "This PrivacyFence does not allow unattended sessions (Admin page, Features). Ask your administrator, or run with --attended." sync/tests/test_preflight.py compares against the constant, so it needs no change.
    5. settings_window_html.py, the welcome banner (~:755-757): replace the sentence "If your IT team gave you an organization config bundle, install it first from the General page; only the connectors it enables are listed." with "The Admin page decides which services are offered." Update tests/unit/test_settings_window_html.py:263's expected phrase to "Admin page".
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
    6. tests/unit/test_daemon_main.py: in the run_app harness (_patch_common, ~3443-3487), stub data_format.migrate_everything (returning []) and data_format.check_not_newer, so run_app tests never walk the source checkout.
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
    Also apply D15 SEC-14 (a legacy bundle's idp wins over the environment; a legacy org bundle without idp refuses the start with SEC-14's message) and SEC-11's rule that a re-import keeps org_server, each with tests.
    Also apply D18: PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL ("true" or "false", anything else is a ConfigurationError) maps to the idp section's trust_unverified_email; IdpConfig gains trust_unverified_email: bool = False, read from the section.
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
    1. org_identity.py: IdpConfig gains admin_emails (each ASCII .strip().lower(), from the section's admin_emails list; a non-ASCII entry never matches). principal_from_claims sets is_admin when the group rule matches, or when isinstance(claims.get("email"), str), the email is ASCII, its .strip().lower() is in admin_emails, and claims.get("email_verified") is True (D14 as amended by SEC-07; never casefold). The three callers (oauth_provider.py:470, routes_org_identity.py:222, routes_org_stepup.py:179) all pass verified ID-token claims and need no change.
    2. Admins by email come only from PRIVACYFENCE_ADMIN_EMAILS (D14): organization bundles are refused from 7.0 (D25), so build_org_bundle.py gains no --idp-admin-email option.
    3. tests/unit/test_org_identity.py:
       - A verified listed email is admin; matching is case-insensitive.
       - email_verified false, missing, or the string "true" is not admin.
       - An unlisted email is not admin.
       - A list-valued email claim is not admin.
       - A configured email with surrounding whitespace or different case still matches.
       - The group rule still works alone and together with the email list.
    4. Run ruff check . and python3 -m pytest tests/unit -q.
    Also apply D15 SEC-07 (the Google hd rule, email_verified in check_authz_policy, ASCII lower with non-ASCII refused), SEC-08 (reserved subs hashed; the startup log line), each with tests. Add the SEC-07 behaviour change to the commit message so p13b can note it in the CHANGELOG.
    Also apply D18's unverified-email rule: check_authz_policy trusts the email domain without email_verified only when IdpConfig.trust_unverified_email is true (the Google hd rule still applies); admins by email never use it; it is set by PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL (or a legacy bundle's idp.trust_unverified_email), with no new build_org_bundle.py option (D25). Tests: Entra-style claims without email_verified are denied by default and allowed with the flag; an email admin without email_verified is never admin, flag or not.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/test_org_identity.py -q -k admin passes
  - ruff check . passes
- id: p20c-provisioning-keys-and-docker
  title: Provisioning key files, and the Peek and Grist variables in the Docker examples
  depends_on:
  - p20b-admins-by-email
  complexity: S
  touches:
  - src/privacyfence/org_mode.py
  - tests/unit/test_org_mode.py
  - src/privacyfence/peek_provisioning.py
  - tests/unit/test_peek_provisioning.py
  - src/privacyfence/grist_provisioning.py
  - tests/unit/test_grist_provisioning.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  - src/privacyfence/admin_config.py
  - tests/unit/test_admin_config.py
  - deploy/docker/privacyfence.env.example
  - deploy/docker/compose.yaml
  brief: |-
    Read "Design → D18": "Provisioning keys (p20c)" and "Docker and docs".
    1. org_mode.py: add provisioning_key(key_env) per D18.
    2. Use it instead of os.environ.get(config.key_env, "") in peek_provisioning.py (~:309), grist_provisioning.py (~:358) and daemon_main.py's run_peek_release/run_grist_release (~:2393, ~:2443), and in admin_config.service_rows' key_set (p6). The startup validation calls it once per enabled provisioner, so a bad _FILE stops startup.
    3. deploy/docker/privacyfence.env.example: a commented "Peek and Grist (optional)" block with PRIVACYFENCE_PEEK_BASE_URL, PRIVACYFENCE_GRIST_SERVER_URL, PRIVACYFENCE_GRIST_PROVISIONER_URL and the two ..._PROVISIONING_KEY_FILE lines, saying automatic accounts are turned on under Admin → Services. compose.yaml: extend the commented secrets: example with peek_provisioning_key and grist_provisioning_key.
    4. Tests: provisioning_key with a plain value, a _FILE, both (ConfigurationError), an unreadable file (message names the variable, not the content); the provisioners and the release commands read a _FILE key; service_rows' key_set follows provisioning_key.
    5. Run ruff check ., python3 -m pytest tests/unit/test_org_mode.py tests/unit/test_peek_provisioning.py tests/unit/test_grist_provisioning.py tests/unit/test_admin_config.py tests/unit/test_daemon_main.py -q, then python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit/test_org_mode.py tests/unit/test_peek_provisioning.py tests/unit/test_grist_provisioning.py tests/unit/test_admin_config.py tests/unit/test_daemon_main.py -q passes
  - grep -n "def provisioning_key" src/privacyfence/org_mode.py matches
  - grep -rn "os.environ.get(config.key_env" src/privacyfence finds nothing
  - grep -n "PRIVACYFENCE_GRIST_PROVISIONER_URL" deploy/docker/privacyfence.env.example matches
  - python3 -m pytest tests/unit -q passes
  - ruff check . passes
- id: p20d-builtin-signin-config
  title: 'Built-in Google sign-in: the opt-in setting and the default-deny sign-in rule'
  depends_on:
  - p20c-provisioning-keys-and-docker
  complexity: M
  worker_model: opus
  worker_model_reason: it changes who can sign in to an organization server; the default-deny and hd rules are security-critical
  touches:
  - src/privacyfence/org_mode.py
  - tests/unit/test_org_mode.py
  - src/privacyfence/org_identity.py
  - tests/unit/test_org_identity.py
  - src/privacyfence/admin_config.py
  - tests/unit/test_admin_config.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  - deploy/docker/privacyfence.env.example
  brief: |-
    Read "Design → D20" ("The client", "Configuration", "Who may sign in", "Admin page"), D14 (idp_section) and SEC-07, SEC-14. The relay wiring is p20e; do not change redirect URIs here.
    1. org_mode.idp_section: PRIVACYFENCE_IDP=builtin-google builds D20's section (there is no bundle form: organization bundles are refused from 7.0, D25) from app_credentials.builtin_app("google_signin"); raise D20's four ConfigurationErrors; keep SEC-14's signed-bundle rule. Any other PRIVACYFENCE_IDP value raises ConfigurationError naming the accepted value.
    2. org_identity: IdpConfig gains builtin: bool and relay: bool from the section; check_authz_policy applies D20's default-deny when builtin (admins by verified email with hd, allowed domains with hd, everyone else refused even with empty authz); trust_unverified_email is refused with builtin.
    3. admin_config: import_from_bundle seeds authz.allowed_domains from PRIVACYFENCE_ALLOWED_DOMAINS (comma-separated, lower-cased, each a valid domain, otherwise ConfigurationError naming the variable) on the first import only; later starts log once that it is ignored.
    4. settings_window_html: the Admin page's Sign-in section shows D20's first line and the read-only admin list when the IdP is built-in.
    5. deploy/docker/privacyfence.env.example: a commented "Built-in Google sign-in (opt-in)" block with PRIVACYFENCE_IDP=builtin-google, PRIVACYFENCE_ADMIN_EMAILS and PRIVACYFENCE_ALLOWED_DOMAINS, saying the own-IdP block is the alternative and the two cannot be combined.
    6. Tests: each ConfigurationError; default-deny for an outside Google account with empty authz; an allowed-domain account with matching hd allowed and with a different hd refused; a consumer account (no hd) refused even when listed in admin_emails; trust_unverified_email refused with builtin; the seed happens once; the Sign-in line renders only for builtin.
    7. Run ruff check ., python3 -m pytest tests/unit/test_org_mode.py tests/unit/test_org_identity.py tests/unit/test_admin_config.py tests/unit/test_settings_window_html.py -q, then python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit/test_org_mode.py tests/unit/test_org_identity.py tests/unit/test_admin_config.py -q passes
  - grep -n "builtin-google" src/privacyfence/org_mode.py deploy/docker/privacyfence.env.example matches in both
  - python3 -m pytest tests/unit -q passes
  - ruff check . passes
- id: p20e-builtin-signin-relay
  title: 'Built-in Google sign-in through the relay: redirects, state, registration and the cross-server checks'
  depends_on:
  - p20d-builtin-signin-config
  complexity: M
  worker_model: opus
  worker_model_reason: it routes IdP sign-in and step-up of every opted-in server through one shared client; the PKCE, nonce and state binding must hold across servers
  touches:
  - src/privacyfence/org_identity.py
  - tests/unit/test_org_identity.py
  - src/privacyfence/web/routes_org_identity.py
  - tests/unit/web/test_routes_org_identity.py
  - src/privacyfence/web/routes_org_stepup.py
  - tests/unit/web/test_routes_org_approvals.py
  - src/privacyfence/web/oauth_provider.py
  - tests/unit/web/test_oauth_provider.py
  - src/privacyfence/relay_client.py
  - tests/unit/test_relay_client.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  brief: |-
    Read "Design → D20" ("Through the relay", "Cross-server safety", "Step-up") and D5 (the relay contract and state format).
    1. In daemon_main (idp_callback_url, ~:1230), routes_org_identity (LOGIN_CALLBACK_PATH) and routes_org_stepup (~:144, ~:169), when idp.relay is true use oauth_apps.relay_redirect_uri("idp" | "idp-login" | "stepup") and a "<netloc>~<token_urlsafe(32)>" state; store that redirect_uri on the pending attempt and use it for the code exchange. Without relay nothing changes.
    2. relay_client: status()["needed"] is true when the IdP is built-in, so registration and the well-known token start at once; register sends signin_domains (D20 "Domain proof for sign-in") and persists the verified set in org/relay_status.json; status() exposes it. /login renders D20's "Sign-in is starting up" page while registration has not succeeded and its "waiting for DNS" page while no domain is verified, and does not redirect to Google then. daemon_main logs one line per missing domain at start and after registration, and gains --relay-dns-records, which prints those lines and exits.
    3. org_identity.check_authz_policy (built-in IdP only): the account's hd must also be in relay_client.status()'s verified sign-in domains.
    4. Tests (step-up ones in tests/unit/web/test_routes_org_approvals.py, next to the existing step-up tests): signin_domains sent and the verified set persisted; an unverified domain's admin refused; the DNS log lines and --relay-dns-records output; the relay redirect_uri is used for both authorize and exchange in all three flows; a code exchanged with another attempt's code_verifier fails; an ID token whose nonce belongs to another attempt is refused; a replayed or unknown state is refused; the starting-up page shows before registration and the Google redirect after; a non-builtin IdP keeps today's redirect URIs byte for byte.
    Also apply SEC-26's DNS part: after a replaced token, the missing-domain lines (step 2) give the new TXT values; test it.
    5. Run ruff check ., python3 -m pytest tests/unit/web/test_routes_org_identity.py tests/unit/web/test_routes_org_approvals.py tests/unit/web/test_oauth_provider.py tests/unit/test_relay_client.py tests/unit/test_daemon_main.py -q, then python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit/web/test_routes_org_identity.py tests/unit/web/test_routes_org_approvals.py tests/unit/web/test_oauth_provider.py tests/unit/test_relay_client.py -q passes
  - grep -n "idp-login" src/privacyfence/web/routes_org_identity.py matches
  - grep -n "stepup" src/privacyfence/web/routes_org_stepup.py matches
  - python3 -m pytest tests/unit -q passes
  - ruff check . passes
- id: p25a-connector-config-store
  title: 'Connector configs: the signed per-provider format, the pinned key, serials, storage, the legacy-bundle import, and the effective config in organization mode'
  depends_on:
  - p20e-builtin-signin-relay
  complexity: M
  touches:
  - src/privacyfence/connector_configs.py
  - tests/unit/test_connector_configs.py
  - tests/fixtures/connector_config/**
  - src/privacyfence/oauth_apps.py
  - tests/unit/test_oauth_apps.py
  - src/privacyfence/daemon_main.py
  - tests/unit/test_daemon_main.py
  - tests/conftest.py
  - scripts/check_coverage_floor.py
  brief: |-
    Read "Design → D25" in full, then D4 and D13.
    1. src/privacyfence/connector_configs.py per D25: parse (strict line order and field rules, at most 8 KB), verify (Ed25519 over every line before "signature:", each ending "\n", via org_bundle_signing's primitives), accept (pin on first use only when the caller confirmed the fingerprint; refuse another key; per-provider serial strictly higher than org/connector_config_serials.json; store unchanged at org/connector_configs/<provider>.txt mode 0600), load_all (re-verify every stored file against the pin; a failure raises ConfigurationError naming the provider), remove (keeps the serials file), import_from_legacy_bundle (each credential section of the adopted bundle into org/connector_configs/<provider>.json with "source": "legacy-bundle" and the bundle's SHA-256, once). The pin starts as the legacy bundle's pinned key when one exists. No function puts a client secret in a message, log line or return value meant for display.
    2. tests/fixtures/connector_config/: a test key (PKCS#8 PEM), inputs and the expected signed file for slack, salesforce (with login_url) and grist (with auth_server_url and server_url). The expected files are generated once by the test helper and checked in; p25c's tools must reproduce them byte for byte.
    3. oauth_apps.effective_org_config: in organization mode a provider's section is its connector config, otherwise its built-in app, otherwise none (D25); the bundle's credential sections are ignored in organization mode. Local mode is unchanged (the bundle). credentials_source gains "connector" for organization mode.
    4. daemon_main.run_app (organization mode): import_from_legacy_bundle on the first start with an adopted bundle; load_all at every start; the legacy-bundle WARNING line; refuse a bundle whose SHA-256 differs from org_bundle_adopted.json's (D25's message). Add --print-env-from-bundle [--idp-secret-file PATH] and --retire-bundle per D25, and make adopt_legacy_files skip when org_bundle_adopted.json records "retired": true.
    5. tests/conftest.py: patch the connector-config paths in the D8 fixture (paths under tmp_path); test_connector_configs is exempt.
    6. scripts/check_coverage_floor.py: src/privacyfence/connector_configs.py at 100.
    7. Tests: every parse rule (order, unknown field, control character, length, size); a good file verifies, a changed byte does not; first upload pins only when confirmed; another key is refused; a lower or equal serial is refused, also after remove; a stored file that no longer verifies refuses the start; the legacy import stores each section once and is not repeated; effective config precedence in org mode (connector over built-in, built-in over none, bundle sections ignored) and unchanged in local mode; the refused new bundle; --print-env-from-bundle prints every variable, never the secret, writes the secret file 0600 and refuses to overwrite it; --retire-bundle refuses while the environment differs from the bundle, and once it succeeds a restart does not adopt org_config.json again while that file stays byte-identical. No caplog record and no output contains a sentinel secret.
    8. Run ruff check ., python3 -m pytest tests/unit/test_connector_configs.py tests/unit/test_oauth_apps.py tests/unit/test_daemon_main.py -q, then python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit/test_connector_configs.py -q --cov=privacyfence.connector_configs --cov-branch --cov-report=term-missing passes and reports 100%
  - python3 -m pytest tests/unit -q passes
  - ruff check . passes
- id: p25b-connector-config-admin
  title: Upload and remove connector configs on Admin → Services
  depends_on:
  - p25a-connector-config-store
  complexity: M
  touches:
  - src/privacyfence/web/routes_admin.py
  - tests/unit/web/test_routes_admin.py
  - src/privacyfence/admin_config.py
  - tests/unit/test_admin_config.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  - src/privacyfence/web/routes_settings.py
  brief: |-
    Read "Design → D25" ("Admin page") and D6.
    1. routes_admin (organization mode): POST /api/admin/connector_config/upload (multipart, one file, at most 8 KB; sensitive with always=True per SEC-04; CSRF; the TOFU confirmation answers 409 with the key fingerprint until the body confirms it, as org_config_upload does; classified bespoke-sensitive) and the action remove_connector_config(service) (sensitive, applies live). Both refresh the shared org_config in place and call connector_registry.evict_all() and relay_client.request_check(), as live actions do. Audit summaries per D25; never the secret.
    2. admin_config.service_rows: app_source "connector" with client_id, serial, signed_at and the key fingerprint in options; never the secret.
    3. settings_window_html: the Services row's source line, the "Upload a connector config" file control (an intercepted upload action in _settings_bridge_shim, like upload_rooms) and "Remove our own app", with the reconnect warning; a link to https://privacyfence.eu/tools/connector-config/ and to the Python tool's docs.
    4. Tests: upload needs a passkey on the default step-up configuration; the 409 confirmation and its fingerprint; a wrong key, a lower serial and a malformed file give 400 with the parser's message; upload and remove change the shared org_config (is the same object) and evict connectors; the state never carries a sentinel secret; the row renders each source line.
    5. Run ruff check . and python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit/web/test_routes_admin.py -q -k connector_config passes and selects at least 6 tests
  - python3 -m pytest tests/unit -q passes
  - ruff check . passes
- id: p25c-connector-config-signing-tools
  title: Sign connector configs with a Python tool or a self-contained browser page
  depends_on:
  - p25a-connector-config-store
  - p23-prerelease-org-image
  complexity: M
  touches:
  - scripts/sign_connector_config.py
  - tests/unit/test_sign_connector_config.py
  - website/tools/connector-config/index.html
  - tests/unit/test_connector_config_web_tool.py
  - scripts/build_site.py
  - tests/unit/test_website_pages.py
  - .github/workflows/build.yml
  - tests/unit/test_org_image_workflows.py
  brief: |-
    Read "Design → D25" ("Tooling", "Cross-language tests", "Security").
    1. scripts/sign_connector_config.py per D25 (keygen and sign; the secret from --client-secret-file or getpass, never argv; output mode 0600; refuse to overwrite). It reproduces p25a's fixtures byte for byte.
    2. website/tools/connector-config/index.html per D25: one self-contained file, inline script and style with the CSP meta tag carrying their sha256 hashes, no external resource, no form submission, WebCrypto Ed25519 import of a PKCS#8 PEM, key generation with a PEM download and fingerprint, the provider-specific fields, the unsupported-browser message, and the "runs only in your browser; the offline copy is in the repository" text. Wire it into scripts/build_site.py's page map without a navigation or footer link, with noindex.
    3. tests/unit/test_sign_connector_config.py: the fixtures reproduced; the secret never in argv (main refuses --client-secret); keygen output parses and refuses to overwrite.
    4. tests/unit/test_connector_config_web_tool.py: the page has no http(s) URL in a src or href of script, link, img or iframe; the CSP meta exists with connect-src 'none' and hashes that match the inline blocks; extract the signing script and run it with node (shutil.which("node"); when missing, skip locally but fail when the CI environment variable is set) to sign the fixture inputs with the fixture key, asserting the output equals the expected file byte for byte.
    5. .github/workflows/build.yml: add scripts/sign_connector_config.py and the page (copied as privacyfence-connector-config-tool.html) to the R2 upload and the stable release assets, next to build_org_bundle.py, per D25's "Release assets". A test (in test_org_image_workflows.py, or a new test_release_assets.py if that file is image-only) pins both entries.
    6. Run ruff check . and python3 -m pytest tests/unit/test_sign_connector_config.py tests/unit/test_connector_config_web_tool.py tests/unit/test_website_pages.py tests/unit/test_build_site.py tests/unit/test_org_image_workflows.py -q.
  acceptance:
  - python3 -m pytest tests/unit/test_sign_connector_config.py tests/unit/test_connector_config_web_tool.py -q passes
  - grep -n "connect-src 'none'" website/tools/connector-config/index.html matches
  - ruff check . passes
- id: p24-slack-limited-mode
  title: 'Slack: search for throttled history reads, and the one-click own app on the Admin page'
  depends_on:
  - p25b-connector-config-admin
  complexity: M
  touches:
  - src/privacyfence/oauth_apps.py
  - tests/unit/test_oauth_apps.py
  - src/privacyfence/connectors/slack.py
  - tests/unit/connectors/test_slack_connector.py
  - src/privacyfence/slack_client.py
  - tests/unit/test_slack_client.py
  - src/privacyfence/admin_config.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  - scripts/qa_fixture_recorder.py
  brief: |-
    Read "Design → D24" in full, and docs/slack-setup.md's "Rate limits and routines".
    1. oauth_apps.slack_limited(section) and slack_manifest(issuer_url) per D24.
    2. The Slack connector in limited mode: channel history through search.messages per D24 (with its stop condition), the result and tool-description notes; thread replies capped with the note; approval cards without a thread preview; permalink lookups search first. Outside limited mode nothing changes; pin that with the existing tests unchanged.
    3. The Admin page's built-in Slack row: D24's line and the "Create your workspace's own Slack app" button with the manifest link, then the copy-sign-upload lines pointing at p25b's upload control.
    4. scripts/qa_fixture_recorder.py: the Slack history cases record the limited-mode (search) path, since the runner uses the built-in app.
    5. Tests: slack_limited for built-in, connector and listed; the manifest's redirect URL and scopes equal slack_client.DEFAULT_USER_SCOPES with no bot scope; limited-mode history calls search.messages and never conversations.history; replies cap at 15; the card omits the preview; the row renders the button with a URL that decodes to the manifest.
    6. Run ruff check . and python3 -m pytest tests/unit -q. Record the Slack fixtures again on the runner with /qa-record slack after the merge (ma7), not here.
  acceptance:
  - python3 -m pytest tests/unit -q -k "slack" passes
  - grep -n "def slack_manifest" src/privacyfence/oauth_apps.py matches
  - ruff check . passes
- id: p26-salesforce-package
  title: 'Salesforce: the package install link and the login URL on the Admin page'
  depends_on:
  - p24-slack-limited-mode
  complexity: S
  touches:
  - src/privacyfence/admin_config.py
  - tests/unit/test_admin_config.py
  - src/privacyfence/oauth_apps.py
  - tests/unit/test_oauth_apps.py
  - src/privacyfence/web/routes_admin.py
  - tests/unit/web/test_routes_admin.py
  - src/privacyfence/web/routes_connect.py
  - tests/unit/web/test_routes_connect.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  brief: |-
    Read "Design → D26".
    1. admin_config: services.salesforce.login_url (schema default ""), set_salesforce_login_url with D26's host rule and exact refusal text, summary `Set the Salesforce login URL to <value>` / `Cleared the Salesforce login URL`.
    2. oauth_apps.effective_org_config: login_url into the Salesforce section per D26 (a connector config's own login_url wins).
    3. routes_admin: the action in ADMIN_ACTIONS (both modes, sensitive, live).
    4. settings_window_html: the Salesforce row's install-link line and copy button when the built-in app carries install_url, and the login URL form.
    5. routes_connect: the "has to install PrivacyFence in your org first" line on /connect?error=salesforce for D26's two error codes.
    6. Tests: each accepted and refused login URL (evil.com, http://, a path, my.salesforce.com.evil.com); precedence; the row renders the link only for the built-in app with install_url; the error line for both codes and not for others.
    7. Run ruff check . and python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit -q -k "salesforce" passes
  - grep -n "set_salesforce_login_url" src/privacyfence/admin_config.py src/privacyfence/web/routes_admin.py matches in both
  - ruff check . passes
- id: p12-adrs
  title: ADRs 0254-0267 and the status lines on the ADRs they amend
  depends_on:
  - p20e-builtin-signin-relay
  - p10-bundle-tooling
  - p11-relay-worker
  - p23-prerelease-org-image
  - p24-slack-limited-mode
  - p25c-connector-config-signing-tools
  - p26-salesforce-package
  complexity: M
  touches:
  - docs/adr/0254-built-in-oauth-apps-ship-in-every-distribution.md
  - docs/adr/0255-org-config-holds-only-startup-settings-and-app-credentials.md
  - docs/adr/0256-operational-settings-live-in-a-daemon-written-admin-config.md
  - docs/adr/0257-administration-is-a-separate-admin-page.md
  - docs/adr/0258-org-servers-use-built-in-apps-through-a-domain-verified-relay.md
  - docs/adr/0259-organization-mode-can-start-from-the-environment-and-run-single-user-on-localhost.md
  - docs/adr/0260-local-mode-is-deprecated-in-7-and-removed-in-8.md
  - docs/adr/0261-7x-is-the-migration-checkpoint-for-8x.md
  - docs/adr/0262-the-6-to-7-upgrade-is-reversible.md
  - docs/adr/0263-an-organization-server-can-start-from-environment-variables.md
  - docs/adr/0264-admins-can-be-named-by-verified-email.md
  - docs/adr/0265-live-checks-run-on-a-dedicated-runner-and-test-the-built-in-apps.md
  - docs/adr/0266-an-organization-server-may-opt-in-to-the-built-in-google-sign-in.md
  - docs/adr/0267-pre-releases-publish-the-organization-image-under-their-exact-version.md
  - docs/adr/0268-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md
  - docs/adr/0269-the-built-in-slack-app-reads-through-search-while-slack-throttles-it.md
  - docs/adr/0208-the-organization-image-is-published-to-ghcr-for-stable-releases-only.md
  - docs/adr/0019-live-connector-credentials-only-on-a-self-hosted-runner.md
  - docs/adr/0181-the-qa-runner-is-reachable-only-from-mains-two-qa-workflows.md
  - docs/adr/0011-org-mode-runs-its-own-oauth-authorization-server.md
  - docs/adr/0200-an-organization-bundle-carries-a-signed-serial-and-an-older-one-is-refused.md
  - docs/adr/README.md
  - docs/adr/0016-org-config-bundle-hash-log-and-signing.md
  - docs/adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md
  - docs/adr/0217-the-org-admin-stats-page-shows-counts-only.md
  - docs/adr/0253-ai-token-proposals-are-off-in-organization-mode-until-the-administrator-turns-them-on.md
  brief: |-
    1. Check `ls docs/adr | tail -3` first. If 0254-0266 are taken by ADRs merged to main since this plan was written, take the next free numbers and use them everywhere; p13a-c read the file names from docs/adr/.
    2. Write the sixteen ADRs in docs/adr/README.md's template: Status (Accepted, today's date, Implemented), Context, Decision, Alternatives considered, Consequences, Verification, Related.
       - Their decisions are this plan's "ADRs" section. Context, alternatives and consequences come from the Goal, the Design (D1-D21), "Accepted risks" and "Risks".
       - Alternatives to record:
         - 0254: fetch the credentials from a PrivacyFence endpoint at runtime; let the relay broker the token exchange and hold the secrets.
         - 0255: keep everything in the signed bundle; a third startup-only file for mode/server/idp; let the daemon write the org's own credentials into an unsigned org_config.json.
         - 0256: refuse to start with legacy keys; keep them as read-only defaults alongside the admin config; keep switches in the bundle for desktop installs.
         - 0257: keep admin sections inside Settings.
         - 0258: no relay; a maintainer-approved list of servers; forwarding to any server that asks.
         - 0259: single-user mode that still needs an IdP; signing in with Google through the relay; a fixed password.
         - 0260: dropping local mode in 7 without a deprecation release; keeping local mode indefinitely.
         - 0261: keeping every converter forever; a per-file version check in 8 instead of one marker; migrating lazily per principal as before.
         - 0263: keeping a signed bundle mandatory for org mode; reading every bundle key from the environment (only the IdP and server need it); mixing bundle and environment fields within the idp section.
         - 0264: the group claim only; any email claim without email_verified; the first person to sign in becomes admin.
         - 0266: no built-in sign-in (every server registers its own IdP app); built-in sign-in on by default; the same Google project as the connector apps; allowing personal Gmail accounts.
         - 0265: reworking the existing runner in place; keeping the live checks on own QA apps; a GitHub-hosted runner with credentials as secrets (ADR 0019 rejected it); opening SSH to the internet instead of Tailscale; minting tokens with loopback CLI flows over an SSH tunnel and copying them by hand (the old runner's way); a QA server with /mcp open; publishing a main-branch image to GHCR for the QA server to pull.
         - 0267: keeping images stable-only and testing each release candidate as a hand-built image; a moving "next" or "alpha" tag; publishing pre-release images to a separate package.
         - 0268: keeping the signed org bundle for servers; client IDs and secrets typed on the Admin page, unsigned; signed JSON with canonicalization (Python and JavaScript must agree on every byte); a signing page served by the PrivacyFence server itself (a compromised server could steal the key); refusing upgraded 6.x servers until their IdP is in the environment.
         - 0269: dropping the built-in Slack app (every workspace registers its own); keeping it with no fallback (history reads 1 per minute); waiting for the Marketplace listing before shipping Slack.
         - 0262: renaming org_config.json in place (breaks a downgrade); sharing the pin files (a 7.x serial would refuse 6.x's older bundle); reading whichever file is newer.
       - 0266 records D20, including its single-point-of-failure risk and the default-deny rule.
       - Fold in D15 where it names an ADR: 0254 (SEC-02 threat, SEC-13 rotation, SEC-22, and D26: the packaged Salesforce External Client App, its install link and the login URL), 0256 (SEC-04 passkey for every sensitive Admin action, SEC-20), 0265 (D17 and D22: the firewall, Tailscale-only administration, pfrunner outside the docker group with one no-argument sudo rule, the shared data directory, /mcp blocked, secrets only in step env and the generated module deleted after each run, a new connector as two PRs), 0258 (D16 relay terms, SEC-05 one account and required reviewers, the Slack/Miro residual risk, SEC-09, SEC-15 wording, SEC-23 notification emails in place of a waiting period), 0259 (SEC-01: single-user mode is no protection against an agent that can run docker), 0261 (SEC-21), 0263 (SEC-14).
       - Link source files, never this plan. Do not mention any model or agent.
    3. Add one Status line to each amended ADR, and nothing else:
       - 0016: "Amended by [0255](...): the signature no longer covers operational settings. Amended by [0262](...): the bundle and pin file names. Amended by [0268](...): organization servers read no bundle; own apps are signed connector configs."
       - 0200: "Amended by [0262](...): the serial pin file name. Amended by [0268](...): connector configs carry per-provider serials."
       - 0011: "Amended by [0263](...): the IdP may come from environment variables."
       - 0016's line also names 0263: "org mode no longer always needs a signed bundle".
       - 0196: "Amended by [0255](...) and [0256](...)."
       - 0208: "Amended by [0267](...): pre-releases publish the image under their exact version tag only."
       - 0253: "Amended by [0256](...): the AI token proposals switch is on the Admin page (scoped_tokens.allow_ai_proposals in org/admin_config.json)."
       - 0217: "Amended by [0257](...): the Stats section is on the Admin page."
       - 0019: "Amended by [0265](...): the runner host. The setup doc this ADR cites is now docs/connector-qa.md."
       - 0181: "Amended by [0265](...): the runner group and label are privacyfence-live."
    4. docs/adr/README.md: add sixteen index rows in the existing format:
       - 0254: "Accepted; relates to 0040"
       - 0255: "Accepted; amends 0016, 0196"
       - 0256: "Accepted; amends 0196, 0253; relates to 0016, 0034, 0068, 0070, 0200"
       - 0257: "Accepted; amends 0217; relates to 0033"
       - 0258: "Accepted; relates to 0011, 0186"
       - 0259: "Accepted; relates to 0011, 0177, 0213"
       - 0260: "Accepted; relates to 0002, 0003"
       - 0261: "Accepted; relates to 0041, 0047, 0171, 0172, 0228, 0236"
       - 0262: "Accepted; amends 0016, 0200; relates to 0261"
       - 0263: "Accepted; amends 0011, 0016"
       - 0264: "Accepted; relates to 0011"
       - 0265: "Accepted; amends 0019, 0181; relates to 0254, 0258"
       - 0266: "Accepted; relates to 0258, 0262, 0263"
       - 0267: "Accepted; amends 0208"
       - 0268: "Accepted; amends 0016, 0200; relates to 0255, 0262, 0263"
       - 0269: "Accepted; relates to 0254, 0268"
    5. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q.
  acceptance:
  - ls docs/adr/0254-*.md docs/adr/0255-*.md docs/adr/0256-*.md docs/adr/0257-*.md docs/adr/0258-*.md docs/adr/0259-*.md docs/adr/0260-*.md docs/adr/0261-*.md docs/adr/0262-*.md docs/adr/0263-*.md docs/adr/0264-*.md docs/adr/0265-*.md docs/adr/0266-*.md docs/adr/0267-*.md docs/adr/0268-*.md docs/adr/0269-*.md lists sixteen files (or the renumbered sixteen)
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
       - §5 "Connector apps": built-in apps through the relay are the default. Give the relay requirements from D5 (an https issuer_url with no path, a server reachable from the internet at /.well-known/privacyfence-relay, allowed through §7's IP restriction, outbound HTTPS to oauth.privacyfence.eu), the 35-day re-verification, that only PrivacyFence's maintainers can list registered hosts (D15 SEC-09), and that your own app goes in the bundle.
       - §6 "Build the bundle": only the startup and credential flags remain; list the moved flags and say they are now on the Admin page; describe the one-time import on upgrade and the "are ignored" warning.
       - "Who may sign in", §10 (install-wide policy), §12 (step-up, including "stricter only from the page; relax by editing org/admin_config.json and restarting"), "Notifications on a phone", §13 (file delivery), audit forwarding and scoped tokens: each now points to the matching Admin page section and says which changes need a restart.
       - A new "Admin page" subsection: who sees it, the sections, step-up.
       - §4 "Identity provider" and "Who is an admin": the environment alternative to the bundle (D14), admins by verified email, that Entra ID admins use the group claim, and that a multi-tenant issuer like accounts.google.com should be paired with allowed domains (Admin → Sign-in) or an Internal consent screen. Note that one Google OAuth client can also serve Peek and Grist sign-in, but a separate client per service is recommended. §6: the bundle is optional when the IdP comes from the environment.
       - §14 "Upgrade" and a new "Going back to 6.x" subsection: 7.x copies org_config.json and its pin files to org_bundle.json and the new pin names, and never changes 6.x's files (D13). To go back, stop 7.x and install the latest 6.x (6.10.0 or newer): it runs with org_config.json as it was, and changes made on the Admin page do not carry back. To re-upgrade from 6.x's current files, delete D13's list of 7.x files first. Use the new file name everywhere the guide installs or copies a bundle (Docker: org/org_bundle.json).
       - Also in §14: upgrading to 8.x requires having run 7.x at least once with "Ready for PrivacyFence 8: yes" on the Admin page. Explain data_format.json (D12), the problems it can report and how to fix each, and that 7.x refuses data from a newer release.
       - §15 Troubleshooting: rows for the relay errors (proof_failed, host_not_public, revoked, too_soon, too_many_new_hosts, the issuer_url message, and the relay's 400 text from D5) and for the "are ignored" warning.
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
       - admin_config.json, relay_token.txt and relay_status.json live in the data volume and are part of the backup; replacing relay_token.txt means re-publishing the built-in sign-in's DNS TXT records (D15 SEC-26).
       - The bundle is credentials-only.
       - A new first section, "Run it on this computer" (open it at http://localhost:8765, not 127.0.0.1, because the Host allowlist and the passkey's rp_id are localhost): the D9 docker run command with the 127.0.0.1 publish and why it matters; the setup code in `docker logs`; passkey enrollment; connecting a service through the relay; adding http://localhost:8765/mcp to an AI client; the lost-passkey recovery from D10; and how to go from single-user mode to a multi-user server (PRIVACYFENCE_IDP_* and a public PRIVACYFENCE_ISSUER_URL in privacyfence.env, or an idp section in a signed bundle).
       - The PRIVACYFENCE_MODE, PRIVACYFENCE_BIND_HOST, PRIVACYFENCE_PORT and PRIVACYFENCE_ISSUER_URL variables.
       - D15 SEC-01's warning, prominently in "Run it on this computer": anyone who can run docker on that computer controls the install; use a separate OS account (rootless Docker) or a VM, or a server. Also SEC-16's note that the setup code stops working once a passkey is enrolled.
    3. configuration-reference.md (and the key-rotation text in org-mode-setup-guide.md): after the upgrade, rotation deletes the new-name pins; the legacy ones only when going back to 6.x is no longer wanted (D15 SEC-12). Also the D9 and D14 environment variables and their precedence (a bundle's idp section wins over PRIVACYFENCE_IDP_*); org_bundle.json's keys (D2), and that it is the 6.x org_config.json copied once (D13); org/admin_config.json (D3 schema; written by the Admin page; hand-edit only to relax step-up, then restart); org/relay_token.txt; the PRIVACYFENCE_BUILTIN_APPS development variable and its JSON shape.
    4. configuration-reference.md: remove the moved build_org_bundle.py flags from its option list (p10 made the reference test skip refused flags) and add a short "Refused since 7.0" note naming them. Say that step_up.rp_id, rp_name and batch have no Admin control and are set by editing org/admin_config.json, then restarting.
    5. how-it-works.md (:277-278), connector-qa.md (:153): where they say operational settings live in the bundle, point to the Admin page and admin_config.json instead. testing-policy.md (:183): add the Admin page to the phone-layout list of app surfaces.
    6. Run the docs tests below, python3 -m pytest tests/unit/test_docs_configuration_reference.py -q, and ruff check ..
    Also apply D25 (operator docs): org-mode-setup-guide.md and org-mode-docker.md describe an organization server with no bundle: privacyfence.env for the server and IdP, and signed connector configs (Admin → Services) for an organization's own apps, with both signing tools, the fingerprint check on the first upload, key rotation by deleting org/connector_config_signing_pubkey.txt, and the serial rule. §14 "Upgrade": a 6.x bundle keeps working read-only for its server and IdP settings, its credentials are imported as connector configs, and privacyfence-app --print-env-from-bundle --idp-secret-file writes the environment to move to before 8.x. The configuration reference lists the connector-config format and files, and marks the org-mode bundle options as refused since 7.0; desktop bundles are unchanged.
    Also apply D20 (operator docs): org-mode-docker.md and org-mode-setup-guide.md present the built-in Google sign-in as the opt-in quick start for small Google Workspace teams (PRIVACYFENCE_IDP=builtin-google, admin emails, allowed domains), state the default-deny rule, that personal Gmail accounts and groups are not supported with it, and the dependency on the relay and the privacyfence-signin project, and recommend the organization's own IdP app otherwise; the configuration reference lists the three variables. Also apply D21 (operator docs): the configuration reference moves --enable/--disable-scoped-token-proposals to the refused list and documents scoped_tokens.allow_ai_proposals in org/admin_config.json (Admin → Scoped tokens). Also apply D18 (operator docs): the configuration reference moves the auto-provisioning rows from the bundle flags to the admin config (Admin → Services) and the environment variables PRIVACYFENCE_PEEK_BASE_URL, PRIVACYFENCE_GRIST_SERVER_URL, PRIVACYFENCE_GRIST_PROVISIONER_URL and <key_env>/<key_env>_FILE; org-mode-docker.md's environment-only quick start shows Peek and Grist as an optional step with the provisioning keys as Docker secrets.
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
         - Google: an unverified-app screen and a 100-user cap until verification, which includes an annual security assessment for Gmail and Drive scopes; a Workspace admin may trust their organization's own client ID, never the built-in one (SEC-02's sentence); an org-mode override must be a Web application client.
         - Salesforce: each org's admin installs the PrivacyFence package once, from the install link on the Admin page, and sets its policy (D26); an org that blocks login.salesforce.com sets its My Domain URL on the Admin page; an organization's own app is a Local External Client App, and the Connected App instructions go.
         - Slack: workspaces that require app approval need an admin's approval once.
         - Atlassian and Miro: nothing extra.
       - Bundle flags only for credentials.
       - grist-setup.md and peek-setup.md: the server address is set on the Admin page (Features); Grist's OAuth app still goes in the bundle.
       - telegram-setup.md: the Admin page switch.
       - google-cloud-setup.md "Room directory sync": the script writes rooms.json (--output) and you upload it on the Admin page.
    4. security-and-compliance.md, a "Built-in apps, the admin config and the relay" subsection:
       - Built-in secrets are public in effect (0254), and secret rotation (D15 SEC-13).
       - Rewrite :19-21 and :66-67 per D15 SEC-01, and state per provider what a compromised relay could do (D15 SEC-05).
       - What the relay sees and does not see (0258).
       - The admin config's settings.yaml-level trust, and that signing now covers only startup settings and credentials (0255, 0256).
    5. Create docs/moving-to-organization-mode.md, the page LOCAL_MODE_DEPRECATION links to (privacyfence.eu/docs/moving-to-organization-mode/):
       - why desktop mode goes away in 8 (ADR 0260);
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
       - "PrivacyFence 7 reads org/org_bundle.json, copied once from org_config.json, and never changes 6.x's files, so you can go back to the latest 6.x (6.10.0 or newer)" (D13);
       - a line at the top of the Unreleased section: "Breaking changes; this release is 7.0.0."
       - No version heading.
    8. Run the docs tests below and ruff check ..
    Also apply D24 (docs): slack-setup.md leads with the built-in app, explains limited mode (history through search, replies capped, why), and makes the workspace's own internal app the full-speed option through the Admin page's one-click manifest and a signed connector config; it keeps "never activate public distribution" for an own app, and says an internal app serves one workspace. The provider setup docs say an organization's own app is a signed connector config on organization servers and a bundle section on desktop installs (D25).
    Also apply D19 (docs): the built-in apps work only in organization mode and the Docker image; slack-setup.md explains the Slack Marketplace rate limit and recommends a workspace's own internal app until PrivacyFence is listed; the CHANGELOG and moving-to-organization-mode.md name the built-in sign-in as available only outside desktop mode. Also apply D18 (user docs): peek-setup.md and grist-setup.md describe automatic accounts as "set the address variables and the key on the server, then turn them on under Admin → Services", and the manual address as either the Admin page or the environment variable; the CHANGELOG notes under Changed, as breaking, that 6.12's --enable/--disable-scoped-token-proposals flags are refused and AI token proposals are switched on Admin → Scoped tokens (D21), and that 6.11's --peek-auto-provision/--grist-auto-provision bundle flags became an Admin-page switch with the addresses from the environment, and that upgraded 6.11 servers keep their automatic accounts with the old addresses locked; it also notes that the built-in Google app shows an unverified-app screen and a 100-user cap until Google verifies it, and points organizations to their own Google app; the CHANGELOG line for SEC-07 names idp.trust_unverified_email / PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL as the setting for Entra ID organizations that use allowed domains.
  acceptance:
  - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_pages.py -q passes
  - grep -n "Admin page" docs/connecting-a-service.md docs/slack-setup.md docs/grist-setup.md matches in each
  - grep -n "moving-to-organization-mode" docs/README.md docs/install-macos.md docs/install-windows.md docs/install-linux.md matches in each
  - git diff origin/main -- CHANGELOG.md | grep -c "^+## \[" prints 0
- id: p13c-website
  title: Relay terms, the privacy policy, the no-relay claims and the desktop-mode notice on the website
  depends_on:
  - p12-adrs
  complexity: M
  touches:
  - website/relay-terms/index.html
  - website/privacy/index.html
  - website/_partials/footer.html
  - website/index.html
  - website/enterprise/index.html
  - website/how-it-works/index.html
  - website/security/index.html
  - website/faq/index.html
  - website/compare/claude-connectors/index.html
  - website/download/index.html
  - scripts/build_site.py
  - tests/unit/test_website_pages.py
  - tests/unit/test_website_claims.py
  brief: |-
    Read "Design → D16" (items 1-6 under "In this PR"), and D15 SEC-09, SEC-15 and SEC-23 for the privacy section. These pages deploy on merge, the same moment the relay goes live; product pages about 7.x features are NOT part of this phase (they are ma5's follow-up).
    1. Create website/relay-terms/index.html from website/imprint/index.html's structure, with D16 item 1's sections. Write them in plain English, keep every point D16 lists, and add an HTML comment at the top: "Not reviewed by a lawyer yet; see the plan's ma5." Link /docs/ (not /docs/oauth-relay/, which does not exist before the 7.0 tag).
    2. Wire it like /imprint/: scripts/build_site.py's page map and llms list, and website/_partials/footer.html (after Imprint, "Relay terms").
    3. website/privacy/index.html per D16 item 3: the scope sentence, the new id="relay" section after the download section, the exception in "The PrivacyFence software is not covered", and "Last updated".
    4. Change each claim D16 item 4 lists, at the lines it gives, to keep the data claim and name the relay. Change only those sentences. Grep website/ (excluding website/_docs/) for "PrivacyFence-operated" and "there are none" afterwards; every remaining hit must be true with the relay live.
    5. website/privacy/index.html: add D16 item 3's id="google-user-data" section, with the Limited Use sentence verbatim.
    6. Add D16 item 5's deprecation notice to website/download/index.html and the FAQ's #modes answer. Use the page's existing notice or callout class if it has one; otherwise a <p class="page-copy"> with <strong>.
    7. Tests per D16 item 6, plus: the privacy page has id="google-user-data" and the exact Limited Use sentence.
    8. Run python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_build_site.py tests/unit/test_website_links.py tests/unit/test_website_canonical_description.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py -q, then python3 -m pytest tests/unit -q -k website and ruff check .. If the hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_website_layout.py tests/integration/test_website_consent.py -q.
    Also apply D20 (privacy and terms): the privacy page's relay section adds that servers using the built-in Google sign-in send their sign-in redirects through the relay, which sees a one-time code and the signing-in account's Google Workspace domain (hd), checks it against the server's DNS-proven sign-in domains, and stores those domains with the registration; the relay terms add that the built-in sign-in client may be suspended or withdrawn like the rest.
  acceptance:
  - python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_build_site.py tests/unit/test_website_links.py tests/unit/test_website_canonical_description.py -q passes
  - grep -n "relay-terms" scripts/build_site.py website/_partials/footer.html matches in both
  - grep -n 'id="relay"' website/privacy/index.html matches
  - grep -n 'Limited Use requirements' website/privacy/index.html matches
  - grep -n "oauth.privacyfence.eu" website/privacy/index.html website/relay-terms/index.html matches in both
  - grep -rln "there are none" website --include=*.html --exclude-dir=_docs finds nothing
  - grep -n "PrivacyFence 8 removes it" website/download/index.html website/faq/index.html matches in both
- id: p21-live-runner
  title: Live checks in organization mode on the new runner (runner.privacyfence.eu), with tokens copied from the QA server, the built-in apps and a relay health check
  depends_on:
  - p22-qa-server-deploy
  complexity: M
  touches:
  - .github/workflows/connector-live-check.yml
  - .github/workflows/qa-record-fixture.yml
  - scripts/qa_fixture_recorder.py
  - tests/unit/test_qa_fixture_recorder.py
  - tests/unit/test_live_check_workflows.py
  - docs/connector-qa.md
  - docs/testing-policy.md
  - docs/coding-and-testing-guidelines.md
  - docs/release-testing.md
  - docs/security-and-compliance.md
  - .claude/commands/qa-record.md
  - .claude/toolkit.yaml
  - .claude/skills/steward/SKILL.md
  - .github/pull_request_template.md
  - tests/fixtures/live/README.md
  - CHANGELOG.md
  brief: |-
    Read "Design → D17" ("What the live checks test: organization mode and the built-in apps"), D22 ("How the runner uses them"), D18's environment addresses, and ADR 0265 (written in p12). The runner itself is set up by hand (manual mb4, ma6); this phase changes only the repository.
    1. scripts/qa_fixture_recorder.py:
       - Add QA_PRINCIPAL_ID = "qa-live", QA_ADMIN (every admin_config.SERVICES entry offered), _qa_context() (enters principal.principal_scope for that principal once, before any connector is built) and _qa_org_config() per D17.
       - Replace every daemon_main.load_org_config() call (~:864, :945, :1283, :1323, :1527, :1790, :1880, :1937, :1984, :2042, :2093, :2171) with _qa_org_config(); where a helper passes org_mode=False to peek_client.bundle_base_url or grist_auth.bundle_settings, pass org_mode=True.
       - main() runs every mode inside _qa_context().
       - Update the module docstring (:1-60): organization mode, the qa-live principal, the token folder users/qa-live/credentials/, minting on the QA org server; and the runner name and label (:4-6, ~:2528-2538, ~:2601).
    2. Both workflows, per D17:
       - runs-on: [self-hosted, privacyfence-live]; the header comments name runner.privacyfence.eu and the privacyfence-live group.
       - Workflow-level env PRIVACYFENCE_PEEK_BASE_URL=https://peek-qa.privacyfence.eu and PRIVACYFENCE_GRIST_SERVER_URL=https://docs.getgrist.com, and PF_QA_PRINCIPAL_ID: ${{ vars.PF_QA_PRINCIPAL_ID }} (empty when unset).
       - A "Write built-in app credentials" step before the recorder: `python3 scripts/builtin_apps.py write` with exactly the ten connector PF_APP_* secrets (GOOGLE_WEB, SLACK, SALESFORCE, ATLASSIAN_RELAY, MIRO; not GOOGLE_SIGNIN) in that step's env only; and a last step with if: always() running `rm -f src/privacyfence/_builtin_apps.py`.
       - The copy-in runs `python3 scripts/qa_runner_tokens.py pull --data-dir "$HOME/qa-org-data"` (p22) and copies ~/privacyfence/tests/fixtures/qa_environment.yaml and, only if present, ~/privacyfence/org/org_bundle.json; the copy-back (if: always()) runs `python3 scripts/qa_runner_tokens.py push --data-dir "$HOME/qa-org-data"`. Neither names a root-level credentials/ or org/org_config.json, and no step writes ~/privacyfence/users/.
       - connector-live-check.yml: the relay health step (continue-on-error) and its line in the job summary.
       - qa-record-fixture.yml's commit message (:247) names the privacyfence-live runner.
    3. tests/unit/test_live_check_workflows.py (new; yaml.safe_load both files): runs-on is exactly ["self-hosted", "privacyfence-live"] for the runner job; the builtin step's env keys are exactly the ten connector PF_APP_* secrets and no PF_APP_ secret appears in workflow-level or job-level env; the cleanup step has if: always() and comes last in the runner job; the copy steps call qa_runner_tokens.py pull and push (push with if: always()) and no step names org/org_config.json or a root-level credentials/; the refusal step of qa-record-fixture.yml still checks refs/heads/main.
    4. tests/unit/test_qa_fixture_recorder.py: _qa_org_config gives the built-in section for a provider the bundle lacks (stub app_credentials.builtin_app) and keeps a bundle section that is present; inside _qa_context, daemon_main._resolve_path(TOKEN_FILES["slack"]) resolves under users/qa-live/credentials/; the Peek and Grist sections take the environment addresses.
    5. Docs per D17 "Docs": rewrite docs/connector-qa.md's runner section (keep its existing requirements; add the host, Cloudflare, Tailscale, pfrunner and Docker separation, organization mode with the qa-live principal, connecting every QA account on the QA org server in a browser and how the runner copies the tokens (D22), the cutover and decommissioning; remove the "Authenticating connectors" CLI section's runner use and the hand-written Grist/Peek/Telegram token instructions), fix :574, and replace the runner name and label in the other listed files at the lines that give them (find them with grep -rn "privacyfence-test" --exclude-dir=.git . and check each hit; tests/integration/test_linux_graphical_session_autostart.py and tests/unit/test_pre_release_check.py contain unrelated identifiers, leave them). Do not put the Tailscale auth key, a runner registration token or any secret value in a doc.
    6. CHANGELOG.md under ## [Unreleased]: one contributor-facing line that the live checks moved to a new runner, run in organization mode with the built-in apps, and check the relay's health.
    7. Run ruff check ., python3 -m pytest tests/unit/test_live_check_workflows.py tests/unit/test_qa_fixture_recorder.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q, then python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit/test_live_check_workflows.py tests/unit/test_qa_fixture_recorder.py -q passes
  - grep -n "qa-live" scripts/qa_fixture_recorder.py .github/workflows/connector-live-check.yml .github/workflows/qa-record-fixture.yml matches in all three
  - grep -rn "privacyfence-test" .github/workflows .claude docs/connector-qa.md docs/testing-policy.md finds nothing
  - grep -n "org/org_config.json" .github/workflows/connector-live-check.yml .github/workflows/qa-record-fixture.yml finds nothing
  - python3 -m pytest tests/unit -q passes
  - ruff check . passes
- id: p22-qa-server-deploy
  title: 'The QA server on the runner: deployed from main, /mcp blocked, token copy for the runner, preview services and the two-PR rule for new connectors'
  depends_on:
  - p13a-docs-operator
  - p13b-docs-user
  complexity: M
  touches:
  - .github/workflows/deploy-qa-server.yml
  - deploy/qa-runner/**
  - scripts/qa_runner_tokens.py
  - tests/unit/test_qa_runner_tokens.py
  - tests/unit/test_qa_runner_deploy.py
  - src/privacyfence/oauth_apps.py
  - tests/unit/test_oauth_apps.py
  - src/privacyfence/admin_config.py
  - tests/unit/test_admin_config.py
  - docs/coding-and-testing-guidelines.md
  - docs/connector-qa.md
  - docs/configuration-reference.md
  - .claude/commands/qa-record.md
  - CLAUDE.md
  - tests/conftest.py
  brief: |-
    Read "Design → D22" in full, and D17's host and runner parts. The host is set up by hand (mb4, ma6); this phase writes the repository side.
    1. .github/workflows/deploy-qa-server.yml per D22: the triggers and concurrency, job build (copy build.yml's build-org-image steps for amd64: the Telegram and built-in credential steps with secrets in step env only, the wheel, the image; tag privacyfence-org:qa-main; docker save | gzip; upload-artifact qa-server-image with retention-days 3), job deploy on [self-hosted, privacyfence-live] (download-artifact to $HOME/qa-deploy/, then exactly `sudo -n /usr/local/sbin/pf-qa-deploy`; no checkout, no secrets), job verify on ubuntu-latest with D22's curl checks. Pin every action by commit SHA as the other workflows do. permissions: contents: read, actions: read.
    2. deploy/qa-runner/: pf-qa-deploy (POSIX sh, set -eu, no arguments, D22's checks in that order), sudoers.pf-qa-deploy, compose.override.yaml, Caddyfile (with {$PF_CF_RANGES}), privacyfence.env.example and README.md, each exactly per D22. No secret value in any file.
    3. scripts/qa_runner_tokens.py (stdlib only) with pull and push per D22, --data-dir and --checkout (default the repository root), reading the token file names from daemon_main.TOKEN_FILES by importing privacyfence.daemon_main inside main(). It never prints a token or file content.
    4. oauth_apps.PREVIEW_SERVICES and PRIVACYFENCE_PREVIEW_SERVICES per D22, applied in effective_org_config and admin_config.service_rows. Add PRIVACYFENCE_PREVIEW_SERVICES to the conftest environment-clearing fixture. Tests: a preview service's section and row are dropped, kept when named, kept with "*", and an empty PREVIEW_SERVICES changes nothing (monkeypatch the frozenset).
    5. tests/unit/test_qa_runner_deploy.py:
       - The workflow: the build job's PF_APP_ and TELEGRAM_ secrets appear only in step env; the deploy job runs on exactly ["self-hosted", "privacyfence-live"], has no checkout step, no secrets and one run step equal to `sudo -n /usr/local/sbin/pf-qa-deploy`; the verify job checks /mcp for 404.
       - The script: no use of "$1" or "$@"; it names the fixed tarball path and privacyfence-org:qa-main; `sh -n` passes.
       - The sudoers line ends with the empty-argument "".
       - The override sets user "10001:10001" and pull_policy never, and mounts /home/pfrunner/qa-org-data.
       - The Caddyfile's @mcp matcher covers routes_mcp.MCP_PATH, oauth_provider.IDP_CALLBACK_PATH, the MCP SDK's AUTHORIZATION_PATH, TOKEN_PATH, REGISTRATION_PATH and REVOCATION_PATH, /oauth/consent/*, and both well-known prefixes. Also build the routes routes_mcp.mount_org_oauth returns (with the test provider test_routes_mcp.py uses) and assert every one of their paths is matched, so a new MCP route cannot slip past.
    6. tests/unit/test_qa_runner_tokens.py per D22's test list, with tmp_path data and checkout directories.
    7. Docs:
       - docs/coding-and-testing-guidelines.md §3 "Adding a connector": D22's two-PR procedure (connect first behind PREVIEW_SERVICES, the browser connection on runner.privacyfence.eu, then tools with /qa-record and removing it from PREVIEW_SERVICES).
       - docs/connector-qa.md: the QA server section (what it is, its deploy from main, /mcp blocked, connecting accounts on /connect, PF_QA_PRINCIPAL_ID), linked from the runner section p21 rewrites.
       - docs/configuration-reference.md: PRIVACYFENCE_PREVIEW_SERVICES.
       - .claude/commands/qa-record.md: a new connector's token must first be connected on the QA server (the connect PR merged).
       - CLAUDE.md: one short "New connectors" bullet under "Commands and skills": a new connector is two PRs, connect then tools, and /make-plan plans them that way with the browser connection between them as a manual step; the procedure is in docs/coding-and-testing-guidelines.md §3.
    8. Run ruff check ., python3 -m pytest tests/unit/test_qa_runner_deploy.py tests/unit/test_qa_runner_tokens.py tests/unit/test_oauth_apps.py tests/unit/test_admin_config.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q, then python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit/test_qa_runner_deploy.py tests/unit/test_qa_runner_tokens.py -q passes
  - grep -n "privacyfence-live" .github/workflows/deploy-qa-server.yml matches
  - grep -n "PREVIEW_SERVICES" src/privacyfence/oauth_apps.py src/privacyfence/admin_config.py matches in both
  - grep -n "two PRs" docs/coding-and-testing-guidelines.md CLAUDE.md matches in both
  - sh -n deploy/qa-runner/pf-qa-deploy passes
  - python3 -m pytest tests/unit -q passes
  - ruff check . passes
- id: p23-prerelease-org-image
  title: Pre-releases publish the organization image under their exact version tag
  depends_on:
  - p1-builtin-apps-generator
  - p11-relay-worker
  complexity: S
  touches:
  - scripts/org_image_tags.py
  - tests/unit/test_org_image_tags.py
  - .github/workflows/publish-org-image.yml
  - tests/unit/test_org_image_workflows.py
  - docs/org-mode-docker.md
  - docs/releasing.md
  brief: |-
    Read "Design → D23" ("Pre-release org images").
    1. scripts/org_image_tags.py: accept the PEP 440 short pre-release form (X.Y.ZaN, bN, rcN) and return exactly [version] for it; stable behaviour unchanged; update the module docstring.
    2. .github/workflows/publish-org-image.yml: remove the stable-only skips; every channel downloads, pushes, tags (through org_image_tags.py) and attests. The summary line for a pre-release says no floating tag moved. Update the header comment (ADR 0267 instead of 0208's stable-only rule).
    3. Tests: test_org_image_tags.py (a pre-release gets [version] even when it is the highest tag; a malformed version is still refused); test_org_image_workflows.py (no step in publish-org-image.yml is gated on the stable channel; the builtin check-dist passes --require on every tag, per p1).
    4. docs/org-mode-docker.md's "Releases" row and image text, and docs/releasing.md's "organization Docker image" paragraph, per D23. Name ADR 0267.
    5. Run ruff check . and python3 -m pytest tests/unit/test_org_image_tags.py tests/unit/test_org_image_workflows.py tests/unit/test_docs_links.py -q.
  acceptance:
  - python3 -m pytest tests/unit/test_org_image_tags.py tests/unit/test_org_image_workflows.py -q passes
  - python3 scripts/org_image_tags.py 7.0.0a1 prints exactly 7.0.0a1 (with the git tags it reads)
  - grep -n "channel == 'stable'" .github/workflows/publish-org-image.yml finds nothing
  - ruff check . passes
- id: p14-retire-plan
  title: Delete the plan document and its manual-steps page
  depends_on:
  - p13a-docs-operator
  - p13b-docs-user
  - p13c-website
  - p21-live-runner
  - p23-prerelease-org-image
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
