# Plan: split the organization config, add an Admin page, and ship built-in OAuth apps

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
- **ADR numbers.** The highest accepted ADR is 0238, so this plan takes 0239 to 0243.
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
- `import_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]` builds a fresh config:
  - **`offered`.** If the bundle has a non-empty section for any service in `SERVICES`, each
    service's `offered` is whether its section is non-empty, as ADR 0196 had it. Otherwise
    google, slack, salesforce, atlassian, miro and telegram are `true`, and grist and peek are
    `false`, because they need a server address.
  - `atlassian.assets`/`assets_workspace_id`, `peek.base_url`, and `grist.server_url` (only when
    the bundle's grist has no `client_id`) are copied from the bundle.
  - Each `OPERATIONAL_SECTIONS` key present in the bundle is copied verbatim.
  - `imported_from_bundle_sha256` is the SHA-256 hex of
    `json.dumps(bundle, sort_keys=True, separators=(",", ":"))`, or `""` for an empty bundle.
- `ensure(bundle) -> tuple[dict[str, Any], bool]`:
  - It returns the loaded config and `False` when the file exists.
  - Otherwise it saves and returns `(import_from_bundle(bundle), True)`.
- `default_config() -> dict[str, Any]` is `import_from_bundle({})`.
- `service_rows(bundle, admin, *, mode)`: see D6.
- `apply_change(action: str, payload: dict[str, Any]) -> tuple[str, list[str]]`: see D6. A
  module-level `threading.Lock`, `_write_lock`, serializes it.
- No function in this module ever puts a value from `audit_forwarding.http.bearer_token_env`'s
  environment variable, or any client secret, into a message, a log line or a return value.

**Where the import runs.** `daemon_main.load_admin_config(bundle) -> dict[str, Any]` is called
once in `run_app`, right after `bundle = load_org_config()`.

- It calls `admin_config.ensure(bundle)`.
- On import it logs at WARNING `Imported <n> settings from org_config.json into
  org/admin_config.json; they are now managed on the Admin page`. It records an audit entry with
  decision `admin_config_imported` and the bundle hash in its summary, after the audit logger is
  initialized, next to `log_org_config_bundle_hash`.
- Whenever `admin_config.legacy_keys_in(bundle)` is non-empty and no import happened, it logs at
  WARNING `org_config.json carries settings that are now managed on the Admin page and are
  ignored: <names>. Rebuild the bundle without them.`
- `AdminConfigError` becomes `org_mode.ConfigurationError` (refuse to start).

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
  `oauth_apps.effective_org_config(load_org_config(), admin_config.load() or admin_config.default_config(), mode="local")`.
  The CLI OAuth runners are loopback flows and always use the local apps.
- `settings_controller._org_config_or_empty` (`:1203`) and `refresh_connectors`'s `work`
  (`:1240`) call `effective_org_config(load_org_config(), admin_config.load() or
  admin_config.default_config())`.
- In org mode the raw `bundle` travels down with a new `bundle` keyword:
  - `run_app` passes it to `_maybe_start_web_server(..., bundle=bundle)` (called at `:2160`);
  - that passes it to `_start_org_web_server(..., bundle=...)` (called at `:801-805`);
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
  - no Cloudflare D1 migrations step; instead, after "Deploy Worker", a "Set revoke token" step that runs
    `printf '%s' "$REVOKE_TOKEN" | npx wrangler secret put REVOKE_TOKEN`, with step-level env
    `REVOKE_TOKEN: ${{ secrets.CF_OAUTH_RELAY_REVOKE_TOKEN }}` plus the two Cloudflare secrets;
  - smoke test `curl --head https://oauth.privacyfence.eu/health`;
  - job names `verify-oauth-relay` and `deploy-oauth-relay` (not `verify`, which the download Worker's workflow already uses).

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
- **Who may use it, org mode.** A signed-in principal with `is_admin`. Anyone else gets 403 for an
  action, and for `GET /admin` an HTML page that says `Only administrators can open this page.`
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
  summary)` (decision `settings_change`), with the summary from `admin_config.apply_change`
  suffixed with ` (admin=<id>)` in org mode and ` (principal=local)` in local mode.
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
    applies_live: bool
```

It holds exactly these actions. "Both" means `{LOCAL_MODE, ORG_MODE}`. Each action's parameters are
the JSON body keys, with these types: `service: str`, `enabled: bool`, `value: str` and so on.

| Action | Parameters | Modes | Sensitive | Live |
|---|---|---|---|---|
| `offer_service` | `service` | both | yes (ADR 0070) | yes |
| `withdraw_service` | `service` | both | no | yes |
| `set_atlassian_assets` | `enabled: bool, workspace_id: str = ""` | both | yes | yes |
| `set_server_address` | `service: str` (`grist` or `peek`), `value: str` | both | yes | yes |
| `set_unattended_sessions` | `enabled: bool` | both | yes | no |
| `clear_rooms` | — | both | no | yes |
| `set_download_delivery` | `inline_max_bytes: int, link_ttl_seconds: int, allow_disk_staging: bool, agent_links: bool, allow_exports: bool` | org | yes | no |
| `set_web_push` | `enabled: bool` | org | yes | no |
| `set_authz` | `allowed_domains: str, groups_claim: str, required_groups: str` (comma- or newline-separated lists) | org | yes | no |
| `tighten_step_up` | `enabled: bool, require_passkey: bool, scope: str` | org | yes | no |
| `set_scoped_tokens` | `enabled: bool, max_days: int` | org | yes | no |
| `set_audit_forwarding` | `enabled: bool, kind: str, syslog_host: str, syslog_port: int, syslog_protocol: str, http_url: str, http_bearer_token_env: str` | org | yes | no |
| `toggle_pii_detection`, `toggle_pii_category`, `set_default_policy`, `set_category_policy` | as today in `routes_settings._apply_org_action` | org | yes | yes |

- **The four privacy actions** (last row) keep their existing implementation: the
  `_apply_org_action` branches move unchanged into `routes_admin`, still calling
  `org_install_policy.apply_change`.
- **Validation.** `admin_config.apply_change(action, payload)` validates by building the candidate
  section and running it through the existing parser, `org_mode.<X>.from_org_config({"<section>":
  candidate})`. Use `step_up_config.StepUpConfig.from_org_config` and
  `scoped_tokens.policy_from_org_config(..., org_mode=True)` the same way.
  - A `ConfigurationError` becomes `AdminConfigError(str(exc).replace("org_config.json's ", ""))`.
  - `set_server_address` validates with `grist_auth.normalize_server_url` or
    `peek_client.normalize_base_url`. An empty value clears the address.
  - `set_atlassian_assets` stores `assets` and `assets_workspace_id`.
  - Unknown services raise `Unknown service.`
- **`tighten_step_up`** accepts only changes that make step-up stricter:
  - `enabled` false→true;
  - `require_passkey` false→true;
  - `scope` moving later in `step_up_config.STEP_UP_SCOPES` (`("writes", "writes_and_pii_reads",
    "writes_and_reads")`, narrowest first).

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

- **Live actions.** For an action marked live, after `apply_change` succeeds:
  - Org mode: `oauth_apps.refresh_in_place(org_config, oauth_apps.effective_org_config(bundle,
    admin_config.load(), mode="org"))`, then `connector_registry.evict_all()`, a new method that
    clears `_hosts` and `_last_used` under the lock, then `relay_client.request_check()`.
  - Local mode: `controller.refresh_connectors()`.
- **Restart-only actions.** These are only saved.
- **Pending restart.** `routes_admin` keeps the admin config as loaded at startup
  (`startup_admin`, passed in by `daemon_main`). The page state's `"restart_needed"` lists the
  labels of the restart-only sections whose stored value differs from `startup_admin`. The page
  shows `Saved. Restart PrivacyFence for these changes to take effect: <labels>.`

**Room directory upload.** `POST /api/admin/rooms/upload` takes a multipart form with one file,
like `org_config_upload` does.

- The file is at most 2 MB and is the JSON `scripts/sync_room_directory.py` now writes:
  `{"rooms": [...], "rooms_synced_at": "<iso>"}`, where `rooms` is a list of objects.
- It is not sensitive (plain directory data, no credential) and is classified
  in the route-classification list `routes_admin` keeps, as ADR 0014 requires. It is
  CSRF-checked.
- Summary: `Replaced the room directory (<n> rooms)`.

**Page state.** `routes_admin` builds:

- `services`: `admin_config.service_rows(bundle, admin, mode=mode)`, one row per `SERVICES` entry
  with these fields:
  - `key`, `label`, `offered`
  - `app_source`: `"bundle"`, `"builtin"` or `"none"`, from `oauth_apps.credentials_source`;
    `"builtin"` for telegram when `telegram_app_credentials()` is not None; `"server"` for grist
    and peek
  - `client_id`: from the bundle or built-in app, `""` otherwise
  - `relay`: `mode == "org" and app_source == "builtin" and key != "telegram"`
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

- **`scripts/build_org_bundle.py`.** The flags that write a legacy key are hidden with
  `help=argparse.SUPPRESS`. They stay parseable only to refuse:
  - `--enable-telegram`, `--enable-peek`, `--enable-grist` and `--peek-base-url`;
  - `--grist-server-url` without `--grist-client-id`;
  - the `--atlassian-assets*` flags;
  - every `--authz-*`, `--step-up-*`, `--unattended-*`, `--scoped-tokens-*`, download-delivery,
    `--no-web-push` and `--audit-forwarding-*` flag.

  When any of them is given, the script prints `error: <flag> has moved to the Admin page (ADR
  0241); remove it.` for each one and exits 2 without writing. Find each flag's exact spelling in
  the script's argparse groups (lines 240-610).
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
`paths.org_dir() / "relay_token.txt"`) to `tmp_path / "relay_token.txt"`. Tests that need built-in
apps patch `builtin_app` themselves.

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
  - It has settings.yaml-level trust.
  - Changes need the install owner (local) or an IdP admin (org), plus step-up for sensitive ones.
  - Step-up can only be made stricter from the page; relaxing it is still a file edit, so ADR 0068
    holds.
  - On a desktop install the owner, not the bundle's author, now decides which services are
    offered.

  Relates to 0016, 0034, 0068, 0070, 0200.
- **0242** — Administration is a separate top-level Admin page; Settings shows every person the
  same personal sections. The org Privacy and Stats sections move to Admin. Amends 0217 (where
  Stats lives); relates to 0033.
- **0243** — Org servers use the built-in apps through the PrivacyFence-hosted redirect relay at
  `oauth.privacyfence.eu`. It forwards only to hosts that proved control of their domain, keeps no
  logs, lists registrations publicly, and maintainers can revoke a host. Relates to 0011, 0186.

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

## Risks and open questions

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
- id: ma1-desktop-builtin-signin
  title: Install a desktop build of the feature branch (from a dispatched build.yml run) with no organization bundle, and sign in to Gmail, Slack, Salesforce, Jira and Miro with the built-in apps
  why: real provider consent screens and loopback redirects cannot run in CI
- id: ma2-org-relay-signin
  title: Deploy the relay from the feature branch with wrangler, then on a test org server whose bundle has only mode/server/idp, register with the relay and connect Gmail, Slack, Salesforce, Jira and Miro through it
  why: the relay round trip needs a public domain, the real Worker and real providers; the deploy workflow only deploys main
- id: ma3-upgrade-and-admin-page
  title: Upgrade an org server that has an old-style bundle, check the one-time import, use the Admin page, and compare Settings for an admin and a normal user
  why: checks the migration and the admin path end to end, with step-up, on a real deployment
verify_after_merge:
- python3 -m mypy src/privacyfence
- python3 scripts/mypy_strict_modules.py
- test ! -d cloudflare/oauth-relay || (cd cloudflare/oauth-relay && npm ci && npm run typecheck && npm test)
final_checks:
- docs/builtin-oauth-apps-plan.md and docs/builtin-oauth-apps-plan-manual-steps.html are deleted, and grep -rn "builtin-oauth-apps-plan" --exclude-dir=.git . finds nothing
- ADRs 0239-0243 (or their renumbered successors) exist, are Accepted, and are listed in docs/adr/README.md's index; ADRs 0016, 0196 and 0217 carry one new Status line each; ADR 0040 is unchanged
- CHANGELOG.md has the new entries under "## [Unreleased]" and no new version heading
- cd cloudflare/oauth-relay && npm ci && npm test && npm run typecheck && npm run dry-run passes
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
  - python3 -m pytest tests/unit/test_builtin_apps_script.py tests/unit/test_app_credentials.py tests/unit/test_telegram_credentials.py tests/unit/test_org_image_workflows.py -q passes
  - grep -c "scripts/builtin_apps.py write" .github/workflows/build.yml .github/workflows/publish-pypi.yml scripts/build_dmg.sh scripts/build_deb.sh scripts/build_installer.ps1 reports at least 1 for each file
  - grep -n "src/privacyfence/_builtin_apps.py" .gitignore MANIFEST.in matches in both files
  - 'the dispatched build.yml run''s "Build .deb" log contains "built-in app google_desktop: included"'
  - ruff check . passes
- id: p2-admin-config-store
  title: 'The admin config file: schema, load/save and the one-time import from a bundle'
  depends_on:
  - p1-builtin-apps-generator
  complexity: M
  touches:
  - src/privacyfence/admin_config.py
  - tests/unit/test_admin_config.py
  - tests/conftest.py
  - scripts/check_coverage_floor.py
  brief: |-
    Read "Design → D2", "D3" and "D8". Names, schema, import rules and messages are all there. This phase adds the module only; service_rows and apply_change come in p6, and no caller changes here.
    1. Create src/privacyfence/admin_config.py with AdminConfigError, SERVICES, SERVICE_LABELS, OPERATIONAL_SECTIONS, LEGACY_BUNDLE_KEYS, admin_config_path, load, save, import_from_bundle, ensure, default_config and legacy_keys_in, exactly as D2 and D3 define them. Use secure_files.atomic_write_json and a module-level threading.Lock _write_lock around save.
    2. tests/conftest.py: add the D8 autouse fixture _isolate_admin_config_and_builtin_apps, modelled on _isolate_whats_new_state. It patches admin_config.admin_config_path and app_credentials.builtin_app.
    3. scripts/check_coverage_floor.py: add "src/privacyfence/admin_config.py" to MODULE_FLOORS at 100, in the entry format the file already uses.
    4. tests/unit/test_admin_config.py:
       - A missing file loads as None.
       - A malformed file, a non-object, or a wrong version raises AdminConfigError without the file content in the message.
       - The saved file has mode 0o600 on POSIX; skip this on Windows the way the existing secure_files tests do.
       - import_from_bundle:
         - an empty bundle offers google, slack, salesforce, atlassian, miro and telegram but not grist or peek;
         - a bundle with only slack and peek sections offers exactly those two, and copies peek.base_url;
         - atlassian assets and assets_workspace_id are copied;
         - grist.server_url is copied only when grist has no client_id;
         - every OPERATIONAL_SECTIONS key is copied verbatim;
         - the hash is the documented SHA-256, and "" for {}.
       - ensure imports and saves when the file is missing, and returns (loaded, False) without touching the file when it exists.
       - legacy_keys_in returns the dotted names, including atlassian.assets and a grist.server_url without client_id, and [] for a credentials-only bundle.
    5. Run ruff check ., python3 -m pytest tests/unit -q (the new autouse fixture must break nothing), and python3 -m mypy src/privacyfence/admin_config.py.
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
       - Call the built-ins as `app_credentials.builtin_app(...)` and `app_credentials.telegram_app_credentials()` (module attribute access, after `from . import app_credentials`), so the conftest patch covers them.
       - Import admin_config at module level; admin_config must not import oauth_apps at module level.
    2. scripts/check_coverage_floor.py: add "src/privacyfence/oauth_apps.py" to MODULE_FLOORS at 100.
    3. tests/unit/test_oauth_apps.py:
       - Legacy keys are dropped: every LEGACY_BUNDLE_KEYS key, atlassian.assets/assets_workspace_id and the API-key grist.server_url are absent from the result even when present in the bundle.
       - A withdrawn service's section is removed even when the bundle carries its credentials.
       - Parametrized over google, slack, salesforce and miro × local and org × (bundle credentials, built-in present, built-in absent): the section's exact contents, including relay True only for built-in in org mode; google sections carry auth_uri/token_uri; salesforce built-in uses consumer_key/consumer_secret.
       - atlassian combines credentials with the admin assets and assets_workspace_id, and has no section without credentials.
       - telegram gets {"enabled": True} only when telegram_app_credentials() is not None.
       - grist: a bundle OAuth app wins over the admin server_url; the admin server_url gives {"server_url": ...}; local with neither gives {"enabled": True}; org with neither gives no section.
       - peek: the same three cases with base_url.
       - Every OPERATIONAL_SECTIONS key is copied from admin.
       - A bundle "relay" key is stripped.
       - Neither input is mutated.
       - refresh_in_place keeps the same object, removes missing keys and updates the others.
       - credentials_source for each case.
    4. Run ruff check ., python3 -m pytest tests/unit/test_oauth_apps.py -q and python3 -m mypy src/privacyfence/oauth_apps.py.
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
    1. Guard grep: `grep -rnE "(not|if|bool\()\s*org_config\b|len\(org_config\)" src/privacyfence`. The planner and the reviewer found only harmless hits: `org_config = org_config or {}` in _maybe_start_web_server, and `org_config or {}` at routes_settings.py:1643. If another hit decides something from org_config being empty or non-empty, stop with status=blocked and name it.
    2. daemon_main.py:
       - Add load_admin_config(bundle) per D3. The import audit entry uses decision "admin_config_imported"; record it right after log_org_config_bundle_hash(bundle), so the audit logger exists.
       - Add effective_org_config(bundle, admin) per D4.
       - In run_app: `bundle = load_org_config()`; reject_removed_tool_annotations(bundle); `admin = load_admin_config(bundle)`; resolve_mode, check_storage_permissions and init_privacy_filter use `bundle`; then `org_config = effective_org_config(bundle, admin)`, which every later use takes. log_org_config_bundle_hash keeps `bundle`.
       - The CLI dispatch near line 2518 uses the local form from D4.
       - Thread `bundle` and `admin` down: new keywords `bundle: dict[str, Any] | None = None` and `startup_admin: dict[str, Any] | None = None` on _maybe_start_web_server and _start_org_web_server, passed from run_app. OrgAuth in web/server.py gets fields `bundle: dict[str, Any]` and `startup_admin: dict[str, Any]` (both `field(default_factory=dict)`), set where OrgAuth is built. Nothing reads them yet; p7 does.
    3. settings_controller.py:
       - _org_config_or_empty (~1203) and refresh_connectors' work (~1240) use `effective_org_config(load_org_config(), admin_config.load() or admin_config.default_config())`. Import both from .daemon_main inside the function, as the existing code does, and keep the existing error handling (an AdminConfigError is handled like ConfigurationError).
       - install_org_config_bytes: after a successful install, if admin_config.legacy_keys_in(data) is non-empty, set self.error to D3's "are ignored" sentence.
    4. Tests.
       - test_daemon_main.py, a class TestAdminConfigStartup:
         - The first start with a bundle carrying authz and a slack section writes admin_config.json with slack offered and authz copied, logs the import warning and records an admin_config_imported audit entry.
         - A second start does not re-import, and logs the "are ignored" warning.
         - A malformed admin_config.json raises ConfigurationError.
       - test_daemon_main.py, a class TestEffectiveOrgConfigWiring:
         - log_org_config_bundle_hash receives the raw bundle (monkeypatch it).
         - With no bundle and a built-in slack app, build_connectors(config, org_config) reaches the Slack branch.
         - The CLI form uses google_desktop even when the bundle says mode org.
         - _start_org_web_server stores bundle and startup_admin on OrgAuth.
       - test_settings_controller.py:
         - With no bundle and a built-in miro app, the snapshot lists Miro and authenticate_connector("miro") no longer sets "Miro isn't enabled in your organization config.".
         - With miro withdrawn in admin_config.json, it does.
         - A bundle upload carrying "authz" installs and sets the "are ignored" message.
       - The p2 conftest fixture makes builtin_app return None by default, so existing tests keep their meaning. Do not weaken any assertion.
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
  brief: |
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
    Read "Design → D6": the action table, the validation and tighten_step_up rules, the summaries, and the page state's services row fields. This phase is pure module code; the routes come in p7.
    1. In src/privacyfence/admin_config.py add:
       - apply_change(action, payload) -> tuple[str, list[str]] for every action in D6's table except the four privacy actions, which stay with org_install_policy. It loads the config (default_config() when missing), validates, mutates, saves under _write_lock, and returns (summary, changed_sections). changed_sections uses the section names: "services", "rooms", "unattended_sessions", "download_delivery", "web_push", "authz", "step_up", "scoped_tokens" and "audit_forwarding".
       - Validation per D6:
         - Each candidate section goes through its parser: org_mode.DownloadDeliveryConfig/WebPushConfig/AuthzPolicyConfig/AuditForwardingConfig.from_org_config, step_up_config.StepUpConfig.from_org_config and scoped_tokens.policy_from_org_config(..., org_mode=True).
         - A ConfigurationError is mapped to AdminConfigError(str(exc).replace("org_config.json's ", "")).
         - The comma- or newline-separated authz lists are split, stripped, and empty entries dropped.
       - The tighten_step_up rule and its exact message.
       - replace_rooms(data: dict[str, Any]) -> str: validates {"rooms": list of objects, "rooms_synced_at": str}, stores them, and returns "Replaced the room directory (<n> rooms)". It raises AdminConfigError("The file is not a room directory written by sync_room_directory.py.") otherwise. The upload route in p7 calls it.
       - service_rows(bundle, admin, *, mode) with exactly D6's row fields. Use oauth_apps.credentials_source, and the bundle or built-in client_id; never a secret.
    2. tests/unit/test_admin_config.py:
       - Each action's stored result, summary and changed_sections.
       - Each parser's error surfaces without the "org_config.json's " prefix (for example a negative inline_max_bytes, an http audit URL, required_groups without groups_claim).
       - tighten_step_up: accepts false→true and a wider scope; refuses true→false, a narrower scope and require_passkey true→false, with the exact message.
       - set_server_address normalizes and clears.
       - An unknown service is refused with "Unknown service.".
       - replace_rooms happy path and refusal.
       - service_rows for each app_source, with the relay flag only in org mode for built-in apps, and grist locked when the bundle has an OAuth app.
       - No row, summary or log record (caplog) contains a client_secret value or a bearer-token env var's value. Set a fake env var to a sentinel and assert it never appears.
    3. Run ruff check ., python3 -m pytest tests/unit/test_admin_config.py -q and python3 -m mypy src/privacyfence/admin_config.py.
  acceptance:
  - python3 -m pytest tests/unit/test_admin_config.py -q --cov=privacyfence.admin_config --cov-branch --cov-report=term-missing passes and reports 100%
  - python3 -m mypy src/privacyfence/admin_config.py passes
  - ruff check . passes
- id: p7-admin-routes
  title: 'The Admin dispatcher for both modes: actions, step-up, audit, live refresh and the room upload'
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
  - src/privacyfence/daemon_main.py
  brief: |-
    Read "Design → D6" in full, except "Rendering", which is p8 and p9b. This phase builds the JSON API; the HTML page comes in p8.
    1. connector_registry.py: add evict_all() (clears _hosts and _last_used under the lock), with a test in tests/unit/test_connector_registry.py.
    2. routes_settings.py: add the keyword `sensitive: bool | None = None` to _needs_step_up and _apply_step_up_gate per D6. Existing callers are unchanged. Add a test in test_routes_settings.py showing that sensitive=True gates an action that is not in _SENSITIVE_ACTIONS, and sensitive=False skips one that is.
    3. Create src/privacyfence/web/routes_admin.py:
       - AdminAction and ADMIN_ACTIONS per D6's table, excluding the four privacy actions (p9a moves them).
       - build_routes(*, controller, sessions, step_up, step_up_origin, require_human_session, startup_admin) for local mode, and build_org_routes(*, sessions, step_up, step_up_origin, connector_registry, org_config, bundle, startup_admin) for org mode.
       - Each mounts `POST /api/admin/{action}`, `POST /api/admin/rooms/upload` and `GET /api/admin/state` (the D6 page state as JSON).
       - Authentication and CSRF: copy how routes_settings.build_routes / build_org_routes authenticate, check CSRF and origin for their settings_action. Org mode answers 403 JSON {"error": "Only administrators can do this."} for a non-admin.
       - An action not valid for the mode gives 404.
       - Step-up, audit and the local require_human_session check follow D6.
       - Live actions refresh per D6: org refresh_in_place + evict_all + relay_client.request_check; local controller.refresh_connectors().
       - restart_needed per D6, comparing admin_config.load() with startup_admin, section by section.
       - The room upload route: multipart and size limit as in routes_settings' org_config_upload, then admin_config.replace_rooms; CSRF-checked and not step-up-gated.
       - Keep a module-level ROUTE_CLASSIFICATION mapping each POST path to "action-dispatcher" or "bespoke-nonsensitive", and raise RuntimeError at build time for an unclassified POST route, as routes_settings does (ADR 0014).
    4. web/server.py:
       - Mount build_routes in local mode, wrapped in _owner_only_routes, next to build_settings_routes.
       - Mount build_org_routes in org mode next to build_org_routes for settings, passing org.org_config (the shared dict), org.bundle, org.startup_admin and org.connector_registry.
       - daemon_main.py passes startup_admin to the local builder through _maybe_start_web_server's existing parameters, added in p4.
    5. tests/unit/web/test_routes_admin.py:
       - Local: owner-only (a non-owner principal gets 404, as Settings does).
       - Each sensitive action needs step-up when step-up is required, and withdraw_service and clear_rooms do not.
       - offer_service saves and calls controller.refresh_connectors.
       - Org:
         - a non-admin gets 403;
         - an admin's withdraw_service removes the section from the shared org_config dict (`is` the same object) and calls evict_all and request_check;
         - set_web_push saves and does not touch org_config, and GET /api/admin/state then lists "Web push" in restart_needed;
         - an AdminConfigError gives 400 with its message;
         - each successful change writes a settings_change audit entry with the summary and "(admin=<id>)";
         - the room upload replaces rooms and refuses a non-room file and a file over 2 MB;
         - org-only actions give 404 in local mode;
         - GET /api/admin/state never contains a client secret: put a sentinel secret in the bundle and assert it is absent from the body.
    6. Run ruff check . and python3 -m pytest tests/unit -q.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - python3 -m pytest tests/unit/web/test_routes_admin.py -q passes and selects at least 14 tests
  - grep -n "ROUTE_CLASSIFICATION" src/privacyfence/web/routes_admin.py matches
  - ruff check . passes
- id: p8-admin-page
  title: The Admin page and nav item, with the Services, Features and organization-config sections
  depends_on:
  - p7-admin-routes
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
    1. web_shell.py: add ORG_ADMIN_NAV_ITEMS and org_nav_items(is_admin), change wrap's is_org test, and add the Admin item to _NAV_ITEMS, all per D6. Change the org nav call sites D6 lists to org_nav_items(principal.is_admin). Tests in tests/unit/test_web_shell.py:
       - org_nav_items(True) ends with Admin and org_nav_items(False) has none;
       - wrap treats both org tuples as org;
       - the local nav has Admin.
    2. settings_window_html.py:
       - build_html gains `surface` and `_capabilities_for` gains the surface argument, per D6.
       - The admin surface's local sections are services, features and org_config; the org sections are services and features (the other org sections are p9b).
       - Move the bundle-upload control from the local Settings surface to the admin surface's org_config section. It still posts to /api/settings/org_config/upload.
       - Add renderServices(state) and renderFeatures(state). Use toggleHtml for offer/withdraw, the D6 status lines and links, and the relay status line. For the Assets, server-address and unattended forms, copy the structure of the Grist API-key modal at settings_window_html.py:1970-2020. For the room directory, a file input that posts to /api/admin/rooms/upload like the org-config upload does, plus "Clear" posting clear_rooms.
    3. routes_settings.py: give _settings_bridge_shim an `endpoint` parameter (default "/api/settings"); the admin surface passes "/api/admin".
    4. routes_admin.py: add GET /admin in both builders.
       - It renders web_shell.wrap(build_html(state, surface="admin", mode=..., is_admin=...)) with active="admin", copying how routes_settings renders GET /settings in each mode.
       - In org mode a non-admin gets a 403 page with "Only administrators can open this page.".
    5. Tests:
       - test_settings_window_html.py:
         - The admin surface's section sets for local, org admin and org non-admin.
         - The settings surface no longer has the bundle upload in local mode.
         - Every D6 services status line renders for its row.
         - The relay line appears only in org mode.
         - A grist row with locked options shows no server form.
       - test_routes_admin.py: GET /admin renders for the owner or admin with the Admin nav item active, and 403 for an org non-admin.
    6. Run ruff check . and python3 -m pytest tests/unit -q. If the session-start hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_browser_smoke.py -q.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -n "def org_nav_items" src/privacyfence/web_shell.py matches
  - grep -n "function renderServices" src/privacyfence/settings_window_html.py matches
  - ruff check . passes
- id: p9a-move-org-admin-sections
  title: Org privacy and stats move from Settings to the Admin dispatcher
  depends_on:
  - p8-admin-page
  complexity: M
  touches:
  - src/privacyfence/web/routes_settings.py
  - src/privacyfence/web/org_settings_scope.py
  - src/privacyfence/web/routes_admin.py
  - tests/unit/web/test_routes_settings.py
  - tests/unit/web/test_routes_org_settings.py
  - tests/unit/web/test_org_settings_scope.py
  - tests/unit/web/test_routes_admin.py
  brief: |-
    Read "Design → D6": the last row of the action table, "Page state" (privacy, stats), and "Rendering" (what the org settings surface loses).
    1. org_settings_scope.py: change toggle_pii_detection, toggle_pii_category, set_default_policy and set_category_policy to `ActionScope(frozenset({LOCAL_MODE}))`. In local mode they stay the owner's own settings.
    2. routes_settings.py:
       - Remove those four from _ORG_ALLOWED_ACTIONS and from _apply_org_action.
       - Remove the admin-only "stats" and the admin privacy data from _org_state; an org admin's Settings state equals a normal user's.
       - test_org_settings_scope.py's projection tests must still pass unchanged.
    3. routes_admin.py:
       - Add the four actions to ADMIN_ACTIONS as org-only, sensitive and live.
       - Move their _apply_org_action branches here unchanged, still calling org_install_policy.apply_change and still mapping PolicyChangeRejected to the same error response.
       - Add "privacy" (the rows _org_state built for admins) and "stats" (org_stats.collect(org_config)) to the org page state.
    4. Tests:
       - Move the existing org privacy-action and stats tests from test_routes_org_settings.py to test_routes_admin.py, adapting only the URL (/api/admin/...) and the state endpoint. Keep every assertion.
       - Add: POST /api/settings/toggle_pii_detection in org mode now gives 404, and an org admin's /api/settings state has no "stats" key.
    5. Run ruff check . and python3 -m pytest tests/unit -q.
    Stop condition: if an existing test asserts that org admins see privacy or stats on /settings for a reason other than the old layout (for example an ADR 0217 verification test), move it to /admin as above. If it cannot be expressed against /admin, stop with status=blocked.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -n "toggle_pii_detection" src/privacyfence/web/routes_admin.py matches
  - python3 -c "from privacyfence.web.routes_settings import _ORG_ALLOWED_ACTIONS as A; assert not {'toggle_pii_detection', 'toggle_pii_category', 'set_default_policy', 'set_category_policy'} & A" exits 0
  - ruff check . passes
- id: p9b-admin-org-sections-ui
  title: 'The org-only Admin sections: delivery, access, step-up, tokens, audit, privacy and stats'
  depends_on:
  - p9a-move-org-admin-sections
  complexity: M
  touches:
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  brief: |-
    Read "Design → D6": the action table's org rows, "Page state" and "Rendering".
    1. settings_window_html.py:
       - Add the org admin sections delivery, access, step_up, tokens, audit, privacy and stats to the admin surface, shown only in org mode for admins, and remove privacy and stats from the org settings surface.
       - delivery, access, tokens, audit: one form per section. Its fields mirror the action's parameters (number inputs for ints, checkboxes for bools, a select for audit kind and syslog protocol, and textareas for the authz lists). It posts the whole section through the existing modal and post() pattern used in p8, and is prefilled from the state.
       - step_up: the form offers only stricter choices: checkboxes that can only go from off to on, and a scope select whose options start at the current scope. Add the note "Relaxing step-up is a file edit (ADR 0068)."
       - privacy and stats: reuse the existing renderPrivacy/renderStats code. Move it so the admin surface renders it; do not duplicate it.
       - The restart banner: when state.restart_needed is non-empty, show "Saved. Restart PrivacyFence for these changes to take effect: <labels>." at the top of the Admin page.
    2. tests/unit/test_settings_window_html.py:
       - Section sets: org admin on the admin surface has all of D6's org sections; org settings has no privacy or stats, for an admin or not.
       - Each form renders with prefilled values from a sample state.
       - The step-up form offers no option looser than the current value.
       - The restart banner renders its labels.
       - No rendered HTML contains a sample secret placed in the state's bundle data.
    3. Run ruff check . and python3 -m pytest tests/unit -q. If the hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_browser_smoke.py -q.
  acceptance:
  - python3 -m pytest tests/unit -q passes
  - grep -n "Restart PrivacyFence for these changes to take effect" src/privacyfence/settings_window_html.py matches
  - ruff check . passes
- id: p10-bundle-tooling
  title: build_org_bundle.py writes only startup settings and credentials; the room sync writes a file to upload
  depends_on: []
  complexity: M
  touches:
  - scripts/build_org_bundle.py
  - tests/unit/test_build_org_bundle.py
  - scripts/sync_room_directory.py
  - tests/unit/test_sync_room_directory.py
  brief: |-
    Read "Design → D2" (which keys stay) and "D7".
    1. scripts/build_org_bundle.py:
       - Hide every flag D7 lists with help=argparse.SUPPRESS, and after parsing refuse each one that was given, with D7's exact message and exit code 2, writing nothing.
       - --merge drops legacy keys with D7's note.
       - Keep the signing code byte-identical (tests/unit/test_build_org_bundle.py cross-checks it).
    2. tests/unit/test_build_org_bundle.py:
       - Delete or rewrite the tests that built operational sections, so they now assert the refusal: one parametrized test named test_moved_flag_is_refused over every moved flag, with the exact message and exit code 2. The refusal must fire even when the other required arguments are given, so pass a valid minimal argument set in each case.
       - A --merge of a bundle with authz and peek drops them and prints the note.
       - A credentials-only bundle still builds and signs as before, and every existing signing and cross-check test still passes.
    3. scripts/sync_room_directory.py: --output replaces --org-config per D7. Remove the bundle-merge and signature handling, refuse --org-config with D7's message, write the rooms file with mode 0600, and update the module docstring and example.
    4. tests/unit/test_sync_room_directory.py: rewrite the merge and signing tests into output-file tests: the written file's shape, mode 0600, and the --org-config refusal. Keep the directory-fetch tests unchanged.
    5. Run ruff check . and python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/test_sync_room_directory.py -q.
    Stop condition: if a moved flag is shared with a key that stays (for example a flag writing both a credential and an operational value), stop with status=blocked and name it.
  acceptance:
  - python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/test_sync_room_directory.py -q passes
  - python3 -m pytest tests/unit/test_build_org_bundle.py -q -k test_moved_flag_is_refused passes and selects at least 10 tests
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
  - cd cloudflare/oauth-relay && npm ci && npm run typecheck && npm test && npm run dry-run passes
  - grep -n "oauth.privacyfence.eu" cloudflare/oauth-relay/wrangler.toml matches
  - grep -n "enabled = false" cloudflare/oauth-relay/wrangler.toml matches
  - grep -n "cloudflare/oauth-relay" .claude/toolkit.yaml .github/workflows/dependency-audit.yml .github/dependabot.yml matches in all three
- id: p12-adrs
  title: ADRs 0239-0243 and the status lines on the ADRs they amend
  depends_on:
  - p9b-admin-org-sections-ui
  - p10-bundle-tooling
  - p11-relay-worker
  complexity: M
  touches:
  - docs/adr/0239-built-in-oauth-apps-ship-in-every-distribution.md
  - docs/adr/0240-org-config-holds-only-startup-settings-and-app-credentials.md
  - docs/adr/0241-operational-settings-live-in-a-daemon-written-admin-config.md
  - docs/adr/0242-administration-is-a-separate-admin-page.md
  - docs/adr/0243-org-servers-use-built-in-apps-through-a-domain-verified-relay.md
  - docs/adr/README.md
  - docs/adr/0016-org-config-bundle-hash-log-and-signing.md
  - docs/adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md
  - docs/adr/0217-the-org-admin-stats-page-shows-counts-only.md
  brief: |-
    1. Check `ls docs/adr | tail -3` first. If 0239-0243 are taken by ADRs merged to main since this plan was written, take the next free numbers and use them everywhere; p13a-c read the file names from docs/adr/.
    2. Write the five ADRs in docs/adr/README.md's template: Status (Accepted, today's date, Implemented), Context, Decision, Alternatives considered, Consequences, Verification, Related.
       - Their decisions are this plan's "ADRs" section. Context, alternatives and consequences come from the Goal, Design D1-D8, "Accepted risks" and "Risks".
       - Alternatives to record:
         - 0239: fetch the credentials from a PrivacyFence endpoint at runtime; let the relay broker the token exchange and hold the secrets.
         - 0240: keep everything in the signed bundle; a third startup-only file for mode/server/idp; let the daemon write the org's own credentials into an unsigned org_config.json.
         - 0241: refuse to start with legacy keys; keep them as read-only defaults alongside the admin config; keep switches in the bundle for desktop installs.
         - 0242: keep admin sections inside Settings.
         - 0243: no relay; a maintainer-approved list of servers; forwarding to any server that asks.
       - Link source files, never this plan. Do not mention any model or agent.
    3. Add one Status line to each amended ADR, and nothing else:
       - 0016: "Amended by [0240](...): the signature no longer covers operational settings."
       - 0196: "Amended by [0240](...) and [0241](...)."
       - 0217: "Amended by [0242](...): the Stats section is on the Admin page."
    4. docs/adr/README.md: add five index rows in the existing format:
       - 0239: "Accepted; relates to 0040"
       - 0240: "Accepted; amends 0016, 0196"
       - 0241: "Accepted; amends 0196; relates to 0034, 0068, 0070, 0200"
       - 0242: "Accepted; amends 0217; relates to 0033"
       - 0243: "Accepted; relates to 0011, 0186"
    5. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q.
  acceptance:
  - ls docs/adr/0239-*.md docs/adr/0240-*.md docs/adr/0241-*.md docs/adr/0242-*.md docs/adr/0243-*.md lists five files (or the renumbered five)
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
  brief: |-
    Reference docs describe today's behaviour and link the ADRs p12 wrote (take their file names from docs/adr/).
    1. org-mode-setup-guide.md:
       - §5 "Connector apps": built-in apps through the relay are the default. Give the relay requirements from D5 (an https issuer_url with no path, a server reachable from the internet at /.well-known/privacyfence-relay, allowed through §7's IP restriction, outbound HTTPS to oauth.privacyfence.eu), the 35-day re-verification, the public /registrations list, and that your own app goes in the bundle.
       - §6 "Build the bundle": only the startup and credential flags remain; list the moved flags and say they are now on the Admin page; describe the one-time import on upgrade and the "are ignored" warning.
       - "Who may sign in", §10 (install-wide policy), §12 (step-up, including "stricter only from the page; relax by editing org/admin_config.json and restarting"), "Notifications on a phone", §13 (file delivery), audit forwarding and scoped tokens: each now points to the matching Admin page section and says which changes need a restart.
       - A new "Admin page" subsection: who sees it, the sections, step-up.
       - §15 Troubleshooting: rows for the relay errors (proof_failed, host_not_public, revoked, too_soon, the issuer_url message, and the relay's 400 text from D5) and for the "are ignored" warning.
    2. org-mode-docker.md: outbound HTTPS to oauth.privacyfence.eu when using built-in apps; admin_config.json lives in the data volume and is part of the backup; the bundle is credentials-only.
    3. configuration-reference.md: org_config.json's remaining keys (D2); org/admin_config.json (D3 schema; written by the Admin page; hand-edit only to relax step-up, then restart); org/relay_token.txt; the PRIVACYFENCE_BUILTIN_APPS development variable and its JSON shape.
    4. Run the docs tests below and ruff check ..
  acceptance:
  - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_pages.py -q passes
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
  - CHANGELOG.md
  brief: |-
    Reference docs describe today's behaviour and link the ADRs p12 wrote.
    1. getting-started.md and connecting-a-service.md:
       - A fresh install offers Google, Slack, Salesforce, Atlassian, Miro and Telegram with built-in apps and no bundle.
       - The Admin page: who sees it; Services and Features; on desktop it is the install owner's.
       - The bundle is now only for your own apps (and org mode's server and IdP).
       - Changing a service's app makes people reconnect.
    2. approvals-and-policy.md: in org mode the install-wide privacy policy is edited on the Admin page; Settings is personal.
    3. Provider setup docs (google-cloud, slack, salesforce, atlassian, miro):
       - Open with "You need this page only to use your organization's own app instead of the built-in one."
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
       - Built-in secrets are public in effect (0239).
       - What the relay sees and does not see (0243).
       - The admin config's settings.yaml-level trust, and that signing now covers only startup settings and credentials (0240, 0241).
    5. CHANGELOG.md under "## [Unreleased]":
       - one entry each for: built-in apps; the Admin page and personal-only Settings; the org config split with the one-time import (naming the moved build_org_bundle.py flags and sync_room_directory.py's --output); and organization servers using built-in apps through the relay;
       - a "Removed" or "Changed" note that build_org_bundle.py refuses the moved flags.
       - No version heading.
    6. Run the docs tests below and ruff check ..
  acceptance:
  - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_pages.py -q passes
  - grep -n "Admin page" docs/connecting-a-service.md docs/slack-setup.md docs/grist-setup.md matches in each
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
         - stores only each registered server's host name and registration dates, shown publicly at /registrations;
         - keeps no request logs;
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
    4. Run python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_build_site.py -q. If the hook's Chromium line had no WARNING, also run python3 -m pytest tests/integration/test_website_layout.py tests/integration/test_website_consent.py -q.
  acceptance:
  - python3 -m pytest tests/unit/test_website_claims.py tests/unit/test_website_pages.py tests/unit/test_build_site.py -q passes
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
