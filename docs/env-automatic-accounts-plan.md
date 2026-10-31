# Automatic Peek and Grist accounts switched on from the environment

## Goal

An organization server, or a single-user install on `localhost`, can start with Peek and Grist
already connected for every person, with no clicks on the Admin page. The `privacyfence-suite`
Docker stack needs this: it starts PrivacyFence, Peek, Grist and the Grist provisioner together,
generates the provisioning keys itself, and hands them to every container.

Two new variables, `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` and
`PRIVACYFENCE_GRIST_AUTO_PROVISION=true`, offer the service and turn **Automatic accounts** on. Like
an address set by the environment, they are then locked on the Admin page. In single-user mode, where
automatic accounts are refused today, the same variables connect Peek and Grist for the one person
(`admin@localhost`), as long as both run on the same computer (`localhost` addresses only).

## Current state

- **Where the settings live.** `offered` and `auto_provision.enabled` live in
  `org/admin_config.json` (`_default_service`, `src/privacyfence/admin_config.py:116-131`). They are
  written only by:
  - bundle import (`import_from_bundle`, `admin_config.py:290-337`);
  - the Admin changes `offer_service` and `set_auto_provisioning` (`_apply_service_toggle` at
    `admin_config.py:645-653`, `_apply_auto_provisioning` at `admin_config.py:724-747`), dispatched by
    `apply_change` (`admin_config.py:875-916`, dispatch block `:897-911`).

  The only seeding from the environment is `PRIVACYFENCE_ALLOWED_DOMAINS` (`admin_config.py:247-287`).
- **The effective config.** `effective_org_config` builds the `offered` dict at `oauth_apps.py:264-270`.
  `_auto_provision_enabled` (`oauth_apps.py:166-167`) requires `enabled`, org mode and not single-user.
  `_peek_section` and `_grist_section` (`oauth_apps.py:170-218`) add `auto_provision` only when the
  address is trusted, that is from the environment or locked by an import. In org mode a Grist OAuth
  app can only come from a connector config: the bundle's Grist section is dropped at
  `oauth_apps.py:255-256`, and the connector is read at `:300`.
- **Who calls `effective_org_config`.** It runs at startup (`daemon_main.py:2373`), in the release
  commands (`daemon_main.py:693-705`), in local-mode settings refreshes
  (`settings_controller.py:1222-1270`), and after "live" Admin changes and connector-config uploads
  (`web/routes_admin.py:379`, `:456`, `:545`, through `refresh_live` at `:586-596`). It does not run
  on Admin page renders: those use `admin_config.service_rows`.
- **The Admin page.** The service row is built by `_service_row` (`admin_config.py:998`; Peek and
  Grist options at `:1052-1076`; `"offered"` at `:1080`). `_auto_provision_state`
  (`admin_config.py:555-580`) gives the reason the switch is unavailable. `admin_config` reads the
  environment addresses through the lazy `_env_addresses()` (`admin_config.py:521-526`), which
  swallows `ConfigurationError`. The JS is `autoProvisionRowHtml` (`settings_window_html.py:2227`;
  the trust checkbox payload at `:2244`) and the Offer switch (`:2274`).
- **The provisioners.** They start once, inside `_start_org_web_server`, at `daemon_main.py:1445-1446`.
  The sections are validated earlier, at `:1368` (Peek) and `:1380` (Grist). In single-user mode
  `idp is None` there (`:1381`). With `web.mcp.enabled` false, there is no web server and no
  provisioning (`daemon_main.py:988-990`).
- **Queueing a person.** `notify_sign_in(principal, email_verified=...)` queues one person
  (`peek_provisioning.py:323`; `grist_provisioning.py:376`, which must run off the event loop:
  existing callers use `asyncio.to_thread`, `routes_org_identity.py:275-277`). The six-hourly sweep
  renews from `users/<id>/credentials/{peek,grist}_provisioning.json`.
- **Single-user sign-in.** It never calls `notify_sign_in`. Its principal is fixed:
  `SINGLE_USER_PRINCIPAL = Principal(id="single-user", email="admin@localhost", display_name="Administrator", is_admin=True)`
  (`web/single_user_auth.py:63-65`). Sessions start in the synchronous `start_session` (`:300-304`),
  called from the async setup-code and passkey handlers (`:394`, `:455`).
- **Address rules.** `http://` is allowed only for `localhost`, `127.0.0.1` and `::1`
  (`peek_client.py:63-84`, `grist_auth.py:72-97`). The suite needs nothing more: behind a proxy it uses
  public `https://` names, and on localhost all containers share one network namespace.
- **Tests.** `tests/conftest.py:177-189` (`_clear_server_environment`) clears a fixed list of server
  variables plus the `PRIVACYFENCE_IDP_` prefix.
- **ADRs that constrain this:**
  - 0196: a service is offered only with a section.
  - 0241/0244: PrivacyFence only calls the provisioners.
  - 0243/0247: provisioning needs a verified email.
  - 0256/0257: startup settings come from the environment; operational settings live in the admin
    config.
  - 0260: single-user runs on localhost.
  - 0264: a server can start from environment variables.

## Design

### Variables

| Variable | Values | Effect |
|---|---|---|
| `PRIVACYFENCE_PEEK_AUTO_PROVISION` | unset or empty: the Admin page decides; `true`: on | Peek is offered and Automatic accounts are on |
| `PRIVACYFENCE_GRIST_AUTO_PROVISION` | the same | Grist is offered and Automatic accounts are on |

- **Parsing.** The value is compared after `strip().lower()`. There is no `false`: removing the
  variable hands the switch back to the Admin page.
- **Stored values are untouched.** These variables never write the stored `offered`,
  `auto_provision.enabled` or `trust_unverified_email`. Removing a variable restores what the Admin
  page had.
- **When they apply.** They take effect only when the web server runs (`web.mcp.enabled`), like the
  provisioners themselves. The docs say they do nothing otherwise.

### Helpers

In `src/privacyfence/oauth_apps.py`, next to `environment_addresses` (`:109`):

```python
_ENV_AUTO_PROVISION = {"peek": "PRIVACYFENCE_PEEK_AUTO_PROVISION", "grist": "PRIVACYFENCE_GRIST_AUTO_PROVISION"}

def environment_auto_provision() -> frozenset[str]:
    """The services whose automatic accounts the environment turns on ("peek", "grist")."""

def check_environment_auto_provision(*, single_user: bool, grist_oauth: bool) -> None:
    """The startup checks below; raises ConfigurationError."""
```

- **`environment_auto_provision`.** A value other than unset, empty or `true` raises
  `ConfigurationError("PRIVACYFENCE_PEEK_AUTO_PROVISION must be true, or unset.")`, or the same text
  with the Grist name. It reads `os.environ` on every call, so there is no module state.
- **`_env_auto_provision`.** In `admin_config.py`, a twin of `_env_addresses()`:
  `_env_auto_provision() -> frozenset[str]` imports `oauth_apps` lazily and returns `frozenset()` on
  `ConfigurationError`.

### Startup checks (`check_environment_auto_provision`, called from `daemon_main`)

`_start_org_web_server` calls it right after `GristProvisioningConfig.from_org_config(org_config)`
(`daemon_main.py:1380`), with `single_user=idp is None` and
`grist_oauth=bool((org_config.get("grist") or {}).get("client_id"))`. It never runs inside
`effective_org_config`, so a runtime refresh cannot raise these errors.

When `"peek"` is in `environment_auto_provision()`:

- `PRIVACYFENCE_PEEK_BASE_URL` must be set, else
  `ConfigurationError("PRIVACYFENCE_PEEK_AUTO_PROVISION=true needs PRIVACYFENCE_PEEK_BASE_URL.")`.
- `org_mode.provisioning_key(<stored key_env>)` must return a key, else
  `ConfigurationError("PRIVACYFENCE_PEEK_AUTO_PROVISION=true needs PRIVACYFENCE_PEEK_PROVISIONING_KEY or PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE.")`.
  The stored `key_env` is read with `admin_config.config_for(...)["services"]["peek"]["auto_provision"]["key_env"]`.
  When it is not the default, the message names `<key_env>` and `<key_env>_FILE` instead.
- In single-user mode, the Peek address's host must be `localhost`, `127.0.0.1` or `::1`, else
  `ConfigurationError("On a single-user install, PRIVACYFENCE_PEEK_AUTO_PROVISION=true needs a localhost address in PRIVACYFENCE_PEEK_BASE_URL.")`.

When `"grist"` is in it:

- Both addresses must be set, else
  `ConfigurationError("PRIVACYFENCE_GRIST_AUTO_PROVISION=true needs PRIVACYFENCE_GRIST_SERVER_URL and PRIVACYFENCE_GRIST_PROVISIONER_URL.")`.
- The key check is the same as Peek's, with the Grist names.
- `grist_oauth` must be false, else
  `ConfigurationError("PRIVACYFENCE_GRIST_AUTO_PROVISION=true cannot be used with a Grist OAuth app.")`.
- In single-user mode, both hosts must be loopback, else
  `ConfigurationError("On a single-user install, PRIVACYFENCE_GRIST_AUTO_PROVISION=true needs localhost addresses in PRIVACYFENCE_GRIST_SERVER_URL and PRIVACYFENCE_GRIST_PROVISIONER_URL.")`.

**Local mode.** In `daemon_main`, right after `mode = org_mode.resolve_mode(org_config)`
(`daemon_main.py:972`): if the mode is not `"org"` and `environment_auto_provision()` is non-empty,
log `logger.warning("%s is ignored outside organization mode.", <names joined by ", ">)` once.

**At runtime.** The Admin connector-config upload refuses a Grist config while
`PRIVACYFENCE_GRIST_AUTO_PROVISION` is set. The check runs in `_accept_connector_config_upload`
(`web/routes_admin.py:492`), before `connector_configs.accept`, with the error
`Grist's automatic accounts are set by PRIVACYFENCE_GRIST_AUTO_PROVISION on the server, which works only without a Grist OAuth app. Remove the variable first.`
The upload's provider is known after parsing. Use the same refusal path as the parser's own errors in
that function.

### Effective config

- **Offered.** In the `offered` dict (`oauth_apps.py:264-270`), a service is offered when the stored
  `offered` is true **or** `name in environment_auto_provision()`, still `and not preview_hidden(name)`.
- **`_auto_provision_enabled`.** It becomes
  `_auto_provision_enabled(admin_service, *, mode, single_user, from_environment: bool)`, which
  returns `mode == "org" and (from_environment or (enabled and not single_user))`.
- **Sections.** `_peek_section` and `_grist_section` pass
  `from_environment=name in environment_auto_provision()`. `key_env` and `trust_unverified_email`
  still come from the stored values, and `provisioner_url` from the environment.

### Admin page

**Row fields.** In `_service_row`, use `_env_auto_provision()` to work out whether a service is
locked. `options.auto_provision` gains `"locked_by": "<variable name>"` (empty when not locked), and
the row gains `"offered_locked_by"` with the same value. When `locked_by` is set:

- the row reports `offered: True`, and `auto_provision` reports `enabled: True`, `available: True`,
  `reason: ""`;
- this wins over every other source and reason, including a connector-config Grist app (which the
  startup check refuses anyway).

**JS.**

- `autoProvisionRowHtml` shows `Automatic accounts: on, set by <variable> on the server.` and no
  toggle.
- The trust checkbox stays, except in single-user mode (`(row.options || {}).single_user`, a new
  boolean on the row), where it has no effect and is hidden.
- The Offer switch is replaced by the text `Offered: set by <variable> on the server.`.

**`apply_change`, for a locked service.**

- `offer_service` and `withdraw_service` are refused with
  `AdminConfigError("<Label> is set by <variable> on the server.")`, where `<Label>` is
  `SERVICE_LABELS[name]`, for example `Peek is set by PRIVACYFENCE_PEEK_AUTO_PROVISION on the server.`
- `set_auto_provisioning` ignores its `enabled` parameter and skips `_auto_provision_state`. It writes
  only `trust_unverified_email`, and returns `("Changed automatic <Label> accounts", ["auto_provision"])`,
  or `("", [])` if nothing changed. It never writes the stored `enabled`.

### Single-user mode

- **At start.** In `_start_org_web_server`, right after `grist_provisioning.start(...)`
  (`daemon_main.py:1446`), the daemon checks
  `if idp is None and oauth_apps.environment_auto_provision():`. It then calls
  `peek_provisioning.notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True)` and the same for
  `grist_provisioning`. Both are no-ops when that provisioner did not start. `SINGLE_USER_PRINCIPAL`
  is imported locally, like the other `.web.*` imports there (`daemon_main.py:1355-1365`).
- **At each sign-in.** In the two async handlers that call `start_session`
  (`single_user_auth.py:394`, `:455`), right after the call:
  `peek_provisioning.notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True)`, then
  `await asyncio.to_thread(grist_provisioning.notify_sign_in, SINGLE_USER_PRINCIPAL, email_verified=True)`.
  This keeps the sweep's "signed in within 90 days" honest.
- **Why `email_verified=True`.** The address `admin@localhost` is fixed in code, not asserted by an
  identity provider. The install has exactly one person, reachable only after the setup code or a
  passkey. Peek and Grist must be on the same computer (startup check above), so no shared server
  ever sees two installs' `admin@localhost`. The suite gives the Peek and Grist install admins other
  addresses, so this account is never refused as an admin. ADR 0272 is the one place this decision
  lives; the sibling OIDC plan references it.
- **Known consequences,** recorded in ADR 0272:
  - Every daemon start counts as a sign-in, so the 90-day inactivity rule does not apply in
    single-user mode.
  - If the install later moves to an identity provider, `users/single-user/` keeps being renewed by
    the sweep for up to 90 days after the last start. The docs say to run `--peek-release single-user`
    and `--grist-release single-user` when moving.
- `--peek-release single-user` and `--grist-release single-user` keep working unchanged.

### Rejected

- **Seeding `admin_config.json` once, like `PRIVACYFENCE_ALLOWED_DOMAINS`.** A compose file is
  re-run with the same environment. A one-time seed would silently diverge from it after the first
  Admin-page click, and a fresh volume would behave differently from an old one. A live, locked value
  matches how `PRIVACYFENCE_PEEK_BASE_URL` already behaves.
- **Checks inside `effective_org_config`.** It runs after runtime changes (uploads, live Admin
  changes), where a startup error would crash a request and leave a state that refuses the next start.
- **A `false` value that forces it off.** Removing the variable is enough.
- **Single-user provisioning against a shared Peek or Grist.** The fixed `single-user` id and
  `admin@localhost` would collide across installs and could adopt someone else's account.

## ADRs

- **0271**: Automatic Peek and Grist accounts can be switched on from the environment, and are then
  locked on the Admin page. The stored values are never written. The checks run at startup only.
- **0272**: A single-user install provisions its one person, `admin@localhost`, as verified, only
  against Peek and Grist on `localhost`, when the environment turns automatic accounts on. This ADR is
  also the reference for treating that principal as verified elsewhere, such as the OIDC provider.

## Manual steps

None. The suite plan's `manual_after` checks a real stack end to end.

## Risks and open questions

- **Merge conflicts with `plan/oidc-provider`.** That plan also edits:
  - `tests/conftest.py` `_clear_server_environment` (it adds a `PRIVACYFENCE_OIDC_` prefix);
  - `tests/unit/test_daemon_main.py`;
  - `single_user_auth.py` (line 301, `email_verified=True`);
  - `CHANGELOG.md`.

  If it merged first, rebase and keep both changes. If a conflict is not mechanical, stop with
  `status=blocked`.
- **The upload refusal path.** If `_accept_connector_config_upload` has no point after parsing where
  the provider is known and before `connector_configs.accept` stores the file, stop with
  `status=blocked` and report its structure.
- **The Admin JS has its own tests** (`tests/unit/test_settings_window_html.py`). If the row markup
  cannot be changed as described without restructuring the page, stop with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: env-automatic-accounts
feature_branch: feature/env-automatic-accounts
max_parallel: 1
verify_after_merge:
  - python3 -m pytest tests/unit/test_oauth_apps.py tests/unit/test_admin_config.py tests/unit/test_peek_provisioning.py tests/unit/test_grist_provisioning.py tests/unit/test_daemon_main.py tests/unit/web/test_single_user_auth.py tests/unit/web/test_routes_admin.py tests/unit/test_settings_window_html.py -q
final_checks:
  - docs/env-automatic-accounts-plan.md is deleted and nothing links to it
  - docs/adr/0271-*.md and docs/adr/0272-*.md exist, are Accepted, and are listed in docs/adr/README.md
  - CHANGELOG.md has entries for both variables under "## [Unreleased]" and no new version heading
phases:
  - id: p1-env-switch
    title: The two variables, their startup checks and the effective config
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/oauth_apps.py
      - src/privacyfence/daemon_main.py
      - tests/conftest.py
      - tests/unit/test_oauth_apps.py
      - tests/unit/test_daemon_main.py
      - CHANGELOG.md
    brief: |
      Read docs/env-automatic-accounts-plan.md: "Variables", "Helpers" (only the oauth_apps part),
      "Startup checks" (except "At runtime") and "Effective config". Every string is quoted from there.
      1. src/privacyfence/oauth_apps.py: _ENV_AUTO_PROVISION, environment_auto_provision() and
         check_environment_auto_provision(*, single_user, grist_oauth), next to environment_addresses
         (oauth_apps.py:109). For the loopback check, compare urllib.parse.urlsplit(url).hostname with
         {"localhost", "127.0.0.1", "::1"}.
      2. oauth_apps.py: the offered dict at oauth_apps.py:264-270, the new from_environment parameter of
         _auto_provision_enabled (oauth_apps.py:166), and both section builders.
      3. daemon_main.py: call check_environment_auto_provision right after
         org_mode.GristProvisioningConfig.from_org_config(org_config) (daemon_main.py:1380), and the
         local-mode warning right after resolve_mode (daemon_main.py:972).
      4. tests/conftest.py, _clear_server_environment (line 177): also clear
         PRIVACYFENCE_PEEK_AUTO_PROVISION and PRIVACYFENCE_GRIST_AUTO_PROVISION (add both names to the
         tuple).
      5. Tests (monkeypatch.setenv; follow the environment_addresses tests in test_oauth_apps.py):
         - tests/unit/test_oauth_apps.py:
           test_env_auto_provision_bad_value, test_env_auto_provision_case_and_space,
           test_peek_section_from_env_ignores_stored_off, test_grist_section_from_env,
           test_env_offers_service, test_single_user_section_with_env, test_single_user_section_without_env,
           and one test per message of check_environment_auto_provision (missing address, missing
           key, custom key_env, Grist OAuth app, single-user non-loopback Peek, single-user
           non-loopback Grist).
         - tests/unit/test_daemon_main.py: test_startup_refuses_auto_provision_without_key (org mode,
           variable set, no key: the start raises the message), and test_local_mode_warns_auto_provision
           (caplog).
      6. CHANGELOG.md, under "## [Unreleased]", "### Added" (create it if missing):
         "- **Automatic accounts from the environment.** `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` and `PRIVACYFENCE_GRIST_AUTO_PROVISION=true` offer Peek or Grist and turn their automatic accounts on, shown as set by the server on Admin → Services."
      7. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence` and
         `python3 scripts/mypy_strict_modules.py`. Stop condition: see Risks (merge conflicts).
    acceptance:
      - python3 -m pytest tests/unit/test_oauth_apps.py tests/unit/test_daemon_main.py -q passes, including every named test
      - grep -n "def check_environment_auto_provision" src/privacyfence/oauth_apps.py finds it
      - grep -n "PRIVACYFENCE_PEEK_AUTO_PROVISION" tests/conftest.py finds it
      - ruff check . and python3 -m mypy src/privacyfence pass

  - id: p2-admin-page
    title: The Admin page shows the variables as set by the server and locks the switches
    depends_on: [p1-env-switch]
    complexity: M
    touches:
      - src/privacyfence/admin_config.py
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/web/routes_admin.py
      - tests/unit/test_admin_config.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/web/test_routes_admin.py
    brief: |
      Read docs/env-automatic-accounts-plan.md: "Helpers" (the admin_config part), "Admin page" and
      "Startup checks" → "At runtime".
      1. admin_config.py: _env_auto_provision(), next to _env_addresses() (admin_config.py:521). In
         _service_row (admin_config.py:998; options at 1052-1076, "offered" at 1080): locked_by,
         offered_locked_by, single_user, and the forced values. Locked wins over every other source.
      2. admin_config.py, apply_change (admin_config.py:875-916, dispatch at 897-911): refuse
         offer_service and withdraw_service for a locked service with the message. For
         set_auto_provisioning on a locked service, call a new
         _apply_locked_trust(config, params) -> tuple[str, list[str]] that writes only
         trust_unverified_email, instead of _apply_auto_provisioning.
      3. settings_window_html.py: autoProvisionRowHtml (line 2227) shows the locked line with no toggle,
         and hides the trust checkbox when row.options.single_user is true. The Offer switch
         (line 2274) is replaced by the locked text when row.offered_locked_by is set. Escape with
         esc() as the surrounding code does.
      4. web/routes_admin.py, _accept_connector_config_upload (line 492): refuse a Grist upload while
         PRIVACYFENCE_GRIST_AUTO_PROVISION is set, with the message, before connector_configs.accept.
         Stop condition: see Risks (the upload refusal path).
      5. Tests:
         - test_admin_config.py: test_locked_row_fields, test_locked_wins_over_connector_source,
           test_locked_refuses_offer_and_withdraw, test_locked_trust_change_writes_only_trust (the
           stored enabled is unchanged), test_locked_trust_change_in_single_user_allowed.
         - test_settings_window_html.py: test_locked_auto_provision_line,
           test_locked_offer_text, test_single_user_hides_trust_checkbox.
         - tests/unit/web/test_routes_admin.py: test_locked_offer_post_refused,
           test_grist_connector_upload_refused_while_env_set (nothing is stored).
      6. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence` and
         `python3 scripts/mypy_strict_modules.py`.
    acceptance:
      - python3 -m pytest tests/unit/test_admin_config.py tests/unit/test_settings_window_html.py tests/unit/web/test_routes_admin.py -q passes, including every named test
      - grep -n "def _env_auto_provision" src/privacyfence/admin_config.py finds it
      - grep -n "PRIVACYFENCE_GRIST_AUTO_PROVISION" src/privacyfence/web/routes_admin.py finds the upload refusal

  - id: p3-single-user
    title: A single-user install provisions its one person when the environment turns automatic accounts on
    depends_on: [p2-admin-page]
    complexity: S
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/web/single_user_auth.py
      - tests/unit/test_daemon_main.py
      - tests/unit/web/test_single_user_auth.py
      - tests/unit/test_peek_provisioning.py
      - tests/unit/test_grist_provisioning.py
      - CHANGELOG.md
    brief: |
      Read docs/env-automatic-accounts-plan.md, section "Single-user mode".
      1. daemon_main.py, _start_org_web_server: right after grist_provisioning.start(...)
         (daemon_main.py:1446), add the `if idp is None and oauth_apps.environment_auto_provision():`
         block with the two notify_sign_in calls. Import SINGLE_USER_PRINCIPAL locally, as the other
         .web.* imports in that function are (daemon_main.py:1355-1365). Add a one-line comment naming
         ADR 0272.
      2. web/single_user_auth.py: in the two async handlers, right after start_session(...)
         (single_user_auth.py:394 and :455), add the Peek call and the awaited asyncio.to_thread Grist
         call exactly as designed. Import peek_provisioning and grist_provisioning at the top. Stop
         condition: if that creates an import cycle (ImportError at test collection), import them
         inside the handlers instead, with a comment saying why.
      3. Tests:
         - tests/unit/web/test_single_user_auth.py: test_setup_code_sign_in_notifies_provisioners and
           test_passkey_sign_in_notifies_provisioners (both calls, single-user principal,
           email_verified=True; monkeypatch both).
         - tests/unit/test_daemon_main.py: test_single_user_start_notifies_with_env (variable set,
           PRIVACYFENCE_PEEK_BASE_URL=http://localhost:7700, key set: called once with the principal)
           and test_single_user_start_does_not_notify_without_env (not called).
         - tests/unit/test_peek_provisioning.py and tests/unit/test_grist_provisioning.py:
           test_single_user_principal_is_provisioned (queued with email_verified=True; the fake client
           writes users/single-user/credentials/peek_token.json, and for Grist grist_token.json).
      4. CHANGELOG.md, "## [Unreleased]", "### Added":
         "- **Peek and Grist on a single-user install.** With the same variables and Peek and Grist on `localhost`, a single-user install connects them for its one person, `admin@localhost`, at start and at each sign-in."
      5. Run `ruff check .` and `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/web/test_single_user_auth.py tests/unit/test_peek_provisioning.py tests/unit/test_grist_provisioning.py -q passes, including every named test
      - grep -n "notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True)" src/privacyfence/daemon_main.py finds the line
      - grep -c "asyncio.to_thread(grist_provisioning.notify_sign_in" src/privacyfence/web/single_user_auth.py prints 2

  - id: p4-retire
    title: Reference docs, ADRs 0271 and 0272, delete the plan
    depends_on: [p3-single-user]
    complexity: S
    touches:
      - docs/peek-setup.md
      - docs/grist-setup.md
      - docs/org-mode-docker.md
      - docs/configuration-reference.md
      - deploy/docker/privacyfence.env.example
      - docs/adr/0271-automatic-accounts-can-be-switched-on-from-the-environment.md
      - docs/adr/0272-a-single-user-install-provisions-its-one-person-as-verified.md
      - docs/adr/README.md
      - docs/env-automatic-accounts-plan.md
    brief: |
      1. docs/peek-setup.md, "Automatic accounts (organization mode)":
         - In step 3, add that `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` does the same from the server's
           environment. The Admin row then shows it as set by the server, and the switch and Offer are
           locked.
         - Rename the section "Automatic accounts".
         - Add a paragraph "On a single-user install": admin@localhost; localhost addresses only;
           connected at start and at each sign-in; the Peek install admin needs another address; every
           start counts as a sign-in; release single-user when moving to an identity provider.
         - Say that the variables do nothing without the web server.
         - In "Troubleshooting → Automatic accounts" (around lines 280 and 326), mention the variable
           next to the Admin switch, and add each new start-up message.

         Make the same changes in docs/grist-setup.md ("Automatic API keys", and troubleshooting around
         line 360), with the Grist names and the connector-upload refusal.
      2. docs/configuration-reference.md (environment variables table), docs/org-mode-docker.md
         ("Environment variables" table and Quick start step 9): add both variables with the Design's
         values-table wording. deploy/docker/privacyfence.env.example, in the "Peek and Grist
         (optional)" block: add commented `# PRIVACYFENCE_PEEK_AUTO_PROVISION=true` and
         `# PRIVACYFENCE_GRIST_AUTO_PROVISION=true`, with a one-line comment each.
      3. Write the two ADRs from the Design and "Rejected" sections, in the template in
         docs/adr/README.md, Status Accepted, and add both to the index. Link source files and the PR,
         never this plan. If 0271 or 0272 is taken by then, use the next free numbers and say so in the
         PR.
      4. Delete docs/env-automatic-accounts-plan.md; `grep -rn "env-automatic-accounts-plan" .` must find
         nothing.
      5. Run `python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_no_history.py -q`.
    acceptance:
      - test ! -e docs/env-automatic-accounts-plan.md
      - grep -rn "env-automatic-accounts-plan" docs src finds nothing
      - grep -n "PRIVACYFENCE_PEEK_AUTO_PROVISION" docs/configuration-reference.md docs/org-mode-docker.md docs/peek-setup.md deploy/docker/privacyfence.env.example finds each file
      - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_no_history.py -q passes
```
