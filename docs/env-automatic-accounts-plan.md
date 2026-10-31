# Automatic Peek and Grist accounts switched on from the environment

## Goal

An organization server, or a single-user install on `localhost`, can start with Peek and Grist
already connected for every person, with no clicks on the Admin page. This is what the
`privacyfence-suite` Docker stack needs: it starts PrivacyFence, Peek, Grist and the Grist
provisioner together, generates the provisioning keys itself, and passes them to every container. Two
new variables, `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` and `PRIVACYFENCE_GRIST_AUTO_PROVISION=true`,
offer the service and turn **Automatic accounts** on, locked like an address set by the environment.
In single-user mode, where automatic accounts are refused today, the same variables connect Peek
and Grist for the one person (`admin@localhost`).

## Current state

- **The Admin page is the only writer.** `offered` and `auto_provision.enabled` live in
  `org/admin_config.json` (`_default_service`, `src/privacyfence/admin_config.py:116-131`). Only
  bundle import (`import_from_bundle`, `admin_config.py:290-337`) and the Admin actions
  `offer_service` / `set_auto_provisioning` (`_apply_service_toggle` at `admin_config.py:645-653`,
  `_apply_auto_provisioning` via `apply_action`, `admin_config.py:883-915`) write them. The only
  environment seeding is `PRIVACYFENCE_ALLOWED_DOMAINS` (`admin_config.py:247-287`).
- **The effective config gates on Admin state.** `_auto_provision_enabled`
  (`src/privacyfence/oauth_apps.py:166-167`) requires `enabled` AND org mode AND not single-user.
  `_peek_section` and `_grist_section` (`oauth_apps.py:170-218`) add `auto_provision` only when the
  address is trusted: from the environment, or locked by an import. Peek and Grist appear at all only
  when `offered` and an address exists (`oauth_apps.py:299-312`).
- **The Admin page explains why the switch is unavailable.** `_auto_provision_state`
  (`admin_config.py:555-580`) gives the reason; single-user is refused with "Automatic accounts need
  people to sign in through an identity provider." The service row's `options.auto_provision` is
  built at `admin_config.py:1052-1076` and rendered by `autoProvisionRowHtml` in
  `src/privacyfence/settings_window_html.py:2225-2250`. The Offer switch is at
  `settings_window_html.py:2272`.
- **The provisioners start once.** `peek_provisioning.start` and `grist_provisioning.start` run once
  in `daemon_main.py:1445-1446`. They do nothing unless the section is enabled, and they warn and skip
  when the key is unset (`peek_provisioning.py:301-320`, `grist_provisioning.py:346-373`).
  `notify_sign_in(principal, email_verified=...)` (`peek_provisioning.py:323`,
  `grist_provisioning.py:376`) queues one person; the six-hourly sweep renews from
  `users/<id>/credentials/{peek,grist}_provisioning.json`.
- **Single-user mode never calls `notify_sign_in`.** Its principal is fixed:
  `SINGLE_USER_PRINCIPAL = Principal(id="single-user", email="admin@localhost", display_name="Administrator", is_admin=True)`
  (`src/privacyfence/web/single_user_auth.py:63-65`). Sessions start in `start_session`
  (`single_user_auth.py:300-304`), called from the setup-code and passkey sign-ins
  (`single_user_auth.py:394`, `:455`).
- **http is allowed only for localhost addresses.** Peek and Grist addresses must be `https://`, or
  `http://` only for `localhost`, `127.0.0.1` and `::1` (`peek_client.py:64-84`,
  `grist_auth.py:72-97`). That is enough for the suite: behind a proxy it uses the public `https://`
  names, and in the localhost stack the containers share one network namespace, so
  `http://localhost:<port>` reaches the other containers. No address rule changes.
- ADRs that constrain this: 0196 (offered only with a section), 0241/0244 (PrivacyFence only calls the
  provisioners), 0243/0247 (provisioning needs a verified email), 0256/0257 (startup settings come
  from the environment, operational settings live in the admin config), 0260 (single-user on
  localhost), 0264 (a server can start from environment variables).

## Design

### Variables

| Variable | Values | Effect |
|---|---|---|
| `PRIVACYFENCE_PEEK_AUTO_PROVISION` | unset or empty: the Admin page decides; `true`: on | Peek is offered and Automatic accounts are on |
| `PRIVACYFENCE_GRIST_AUTO_PROVISION` | the same | Grist is offered and Automatic accounts are on |

The value is compared after `strip().lower()`. Any other value refuses the start with
`ConfigurationError("PRIVACYFENCE_PEEK_AUTO_PROVISION must be true, or unset.")` (Grist: same text
with its name). There is no `false`: leaving the variable out hands the switch back to the Admin
page. The stored `auto_provision.enabled` and `offered` are never written by these variables, so
removing a variable restores whatever the Admin page had.

### New module-level helper (`src/privacyfence/oauth_apps.py`)

```python
_ENV_AUTO_PROVISION = {"peek": "PRIVACYFENCE_PEEK_AUTO_PROVISION", "grist": "PRIVACYFENCE_GRIST_AUTO_PROVISION"}

def environment_auto_provision() -> frozenset[str]:
    """The services whose automatic accounts the environment turns on ("peek", "grist")."""
```

It raises `ConfigurationError` for a bad value, as above. It reads `os.environ` on every call (no
module-level cache, so no `tests/conftest.py` reset is needed).

### Startup checks (`effective_org_config`, `oauth_apps.py`)

When `"peek"` is in `environment_auto_provision()`:

- `PRIVACYFENCE_PEEK_BASE_URL` must be set (`"peek" in environment_addresses()`), else
  `ConfigurationError("PRIVACYFENCE_PEEK_AUTO_PROVISION=true needs PRIVACYFENCE_PEEK_BASE_URL.")`.
- `org_mode.provisioning_key(<the stored key_env>)` must return a key, else
  `ConfigurationError("PRIVACYFENCE_PEEK_AUTO_PROVISION=true needs PRIVACYFENCE_PEEK_PROVISIONING_KEY or PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE.")`.
  When the stored `key_env` is not the default, the message names `<key_env>` and `<key_env>_FILE`
  instead.

When `"grist"` is in it:

- `PRIVACYFENCE_GRIST_SERVER_URL` and `PRIVACYFENCE_GRIST_PROVISIONER_URL` must both be set, else
  `ConfigurationError("PRIVACYFENCE_GRIST_AUTO_PROVISION=true needs PRIVACYFENCE_GRIST_SERVER_URL and PRIVACYFENCE_GRIST_PROVISIONER_URL.")`.
- The key check is the same as Peek's, with the Grist names.
- If the Grist section comes from a bundle or connector-config OAuth app (`bundled.get("client_id")`
  in `_grist_section`, or a Grist connector config), the start is refused with
  `ConfigurationError("PRIVACYFENCE_GRIST_AUTO_PROVISION=true cannot be used with a Grist OAuth app.")`.

These run in org mode only. In local (desktop) mode a set variable is ignored with one
`logger.warning("%s is ignored outside organization mode.", <name>)`.

### Effective config

- `offered`: for a service in `environment_auto_provision()`, the effective config treats the service
  as offered whatever `services.<name>.offered` says.
- `_auto_provision_enabled(admin_service, *, mode, single_user, from_environment: bool)`: true when
  `mode == "org"` and either `from_environment`, or `enabled and not single_user`. So the environment
  turns provisioning on in single-user mode too, and the Admin switch still cannot.
- `_peek_section` / `_grist_section` pass `from_environment=name in environment_auto_provision()`.
  The rest of the section (`key_env`, `trust_unverified_email` from the stored values, and
  `provisioner_url` from the environment) is unchanged.

### Admin page

`options.auto_provision` gains `"locked_by": "<variable name>"` (empty string when not locked), and
the service row gains `"offered_locked_by"` with the same value. When `locked_by` is set:

- `available` is true, `enabled` is true, `reason` is `""`.
- `autoProvisionRowHtml` shows the line
  `Automatic accounts: on, set by <variable> on the server.` and no toggle. The trust checkbox stays
  (it is still an Admin-page setting).
- The Offer switch for that row is replaced by the text `Offered: set by <variable> on the server.`.
- `apply_action` refuses `offer_service`, `withdraw_service` and `set_auto_provisioning` for that
  service with `AdminConfigError("<Label> is set by <variable> on the server.")`, for example
  `Peek is set by PRIVACYFENCE_PEEK_AUTO_PROVISION on the server.` A `set_auto_provisioning` call that
  only changes `trust_unverified_email` (with `enabled: true`) is still allowed.

### Single-user mode

- After `peek_provisioning.start(...)` and `grist_provisioning.start(...)` in `daemon_main.py`, when
  `org_mode.is_single_user(...)` is true, the daemon calls
  `peek_provisioning.notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True)` and the same for
  `grist_provisioning`. Both are no-ops when that provisioner did not start.
- `start_session` in `single_user_auth.py` calls the same two functions after it creates the session,
  so the sweep's "signed in within 90 days" counts passkey sign-ins.
- `email_verified=True` is deliberate. The address `admin@localhost` is fixed in code, not asserted by
  an identity provider, and the install has exactly one person. The suite gives the Peek and Grist
  install admins different addresses, so this account is not refused as an admin (ADR below).
- `--peek-release single-user` and `--grist-release single-user` keep working unchanged.

### Rejected

- **Seeding `admin_config.json` once, like `PRIVACYFENCE_ALLOWED_DOMAINS`.** A compose file is
  re-run with the same environment. A one-time seed would silently diverge from it after the first
  Admin-page click, and a fresh volume would behave differently from an old one. A live, locked value
  matches how `PRIVACYFENCE_PEEK_BASE_URL` already behaves.
- **A `false` value that forces it off.** Removing the variable is enough. A third state would need
  its own Admin-page wording for no use case.
- **A configurable single-user email.** `admin@localhost` works with Peek and Grist; the suite gives
  their admins other addresses instead.

## ADRs

- **0271**: Automatic Peek and Grist accounts can be switched on from the environment, and are then
  locked on the Admin page.
- **0272**: A single-user install provisions its one person, `admin@localhost`, as verified, when the
  environment turns automatic accounts on.

## Manual steps

None. The suite plan's `manual_after` checks a real stack end to end.

## Risks and open questions

- **`effective_org_config` is called from more places than startup.** It runs on Admin-page renders
  too. The phase must raise the new `ConfigurationError`s only where `environment_addresses()` already
  raises today (same call path), so a running server never starts raising on a page render. If
  `environment_addresses()` turns out not to be called at startup on every path, stop with
  `status=blocked`.
- **Single-user state and its data-directory guards.** `daemon_main.py:1287-1313` refuses a data
  directory that has an `org_server` mark. Provisioning state files under `users/single-user/` must
  not trip any of those guards. If a test shows they do, stop with `status=blocked`.
- **The Admin JS has its own tests.** `tests/unit/test_settings_window_html.py` checks rendered
  snippets. If the row markup is built in a way the brief's steps do not match, stop with
  `status=blocked` rather than restructure the page.

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
    title: PRIVACYFENCE_PEEK_AUTO_PROVISION and PRIVACYFENCE_GRIST_AUTO_PROVISION turn automatic accounts on, locked on the Admin page
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/oauth_apps.py
      - src/privacyfence/admin_config.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_oauth_apps.py
      - tests/unit/test_admin_config.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/web/test_routes_admin.py
      - CHANGELOG.md
    brief: |
      Read docs/env-automatic-accounts-plan.md, sections "Variables", "New module-level helper",
      "Startup checks", "Effective config" and "Admin page". Every string below is quoted from there.
      Do not implement the "Single-user mode" section (phase p2), except that _auto_provision_enabled
      gets its new parameter here.
      1. In src/privacyfence/oauth_apps.py add _ENV_AUTO_PROVISION and environment_auto_provision()
         next to environment_addresses() (oauth_apps.py:109). Raise ConfigurationError with the
         exact message for a value other than unset, empty or "true" (case- and space-insensitive).
      2. Change _auto_provision_enabled (oauth_apps.py:166) to take a keyword
         from_environment: bool and return
         mode == "org" and (from_environment or (enabled and not single_user)). Pass
         from_environment=name in environment_auto_provision() from _peek_section and _grist_section.
         Where the effective config decides whether a service is offered (oauth_apps.py:299-312),
         treat a service in environment_auto_provision() as offered.
      3. In effective_org_config, next to the existing environment_addresses() call, add the
         startup checks: the missing-address messages, the missing-key messages (use
         org_mode.provisioning_key(stored key_env)), the Grist OAuth app refusal, and the
         local-mode warning. Use the exact texts from the Design.
         Stop condition: if environment_addresses() is not on the same call path as startup (see
         Risks), stop with status=blocked.
      4. In src/privacyfence/admin_config.py, in the service-row builder (admin_config.py:1052-1076),
         add "locked_by" to options["auto_provision"] and "offered_locked_by" to the row. When it is
         set, force available=True, enabled=True and reason="", and report offered=True.
         _auto_provision_state stays as it is for the unlocked case.
      5. In apply_action (admin_config.py:883-915), before dispatching offer_service,
         withdraw_service and set_auto_provisioning, refuse a locked service with
         AdminConfigError("<Label> is set by <variable> on the server."), where <Label> is
         SERVICE_LABELS[name]. Still allow set_auto_provisioning with enabled=True that changes only
         trust_unverified_email.
      6. In src/privacyfence/settings_window_html.py, autoProvisionRowHtml (line 2225): when
         auto.locked_by is set, render the line
         "Automatic accounts: on, set by " + auto.locked_by + " on the server." with no toggle, and
         keep the trust checkbox. Where the Offer switch is built (line 2272), render
         "Offered: set by " + row.offered_locked_by + " on the server." instead of the switch when
         row.offered_locked_by is set. Escape with esc() as the surrounding code does.
      7. Tests (monkeypatch.setenv; follow the existing environment_addresses tests in
         tests/unit/test_oauth_apps.py):
         - test_oauth_apps.py: bad value refused; "TRUE " accepted; Peek section has auto_provision
           with the variable set and the stored enabled=False, offered=False; each missing-address and
           missing-key message; the Grist OAuth app refusal; the local-mode warning (caplog);
           single_user=True with the variable gives auto_provision, without it does not.
         - test_admin_config.py: the row's locked_by/offered_locked_by; apply_action refuses
           offer_service, withdraw_service and set_auto_provisioning(enabled=False) for a locked
           service and allows the trust-only change.
         - test_settings_window_html.py: the locked line and the locked Offer text are rendered.
         - tests/unit/web/test_routes_admin.py: a POST of set_auto_provisioning for a locked service
           answers with the refusal message.
      8. CHANGELOG.md, under "## [Unreleased]" ("### Added", create it if missing):
         "- **Automatic accounts from the environment.** `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` and `PRIVACYFENCE_GRIST_AUTO_PROVISION=true` offer Peek or Grist and turn their automatic accounts on, shown as set by the server on Admin → Services."
      9. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`
         and `python3 scripts/mypy_strict_modules.py`.
    acceptance:
      - python3 -m pytest tests/unit/test_oauth_apps.py tests/unit/test_admin_config.py tests/unit/test_settings_window_html.py tests/unit/web/test_routes_admin.py -q passes
      - grep -n "def environment_auto_provision" src/privacyfence/oauth_apps.py finds one line
      - grep -n "must be true, or unset." src/privacyfence/oauth_apps.py finds the message
      - grep -n "is set by" src/privacyfence/admin_config.py finds the refusal
      - ruff check . and python3 -m mypy src/privacyfence pass

  - id: p2-single-user
    title: A single-user install provisions its one person when the environment turns automatic accounts on
    depends_on: [p1-env-switch]
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
      1. In src/privacyfence/daemon_main.py, right after peek_provisioning.start(...) and
         grist_provisioning.start(...) (daemon_main.py:1445-1446), when org_mode.is_single_user(...)
         is true (use the same arguments as the existing single-user check in daemon_main.py), call
         peek_provisioning.notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True) and
         grist_provisioning.notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True). Import
         SINGLE_USER_PRINCIPAL from privacyfence.web.single_user_auth. Add a one-line comment that
         the address is fixed in code, not asserted by an identity provider (ADR 0272).
      2. In src/privacyfence/web/single_user_auth.py, at the end of start_session
         (single_user_auth.py:300-304), call the same two functions. Import the modules at the top of
         the file. Stop condition: if that import creates an import cycle (ImportError at test
         collection), import them inside start_session instead, with a comment saying why.
      3. Tests:
         - tests/unit/web/test_single_user_auth.py: after a setup-code sign-in and after a passkey
           sign-in, both notify_sign_in functions are called once with the single-user principal and
           email_verified=True (monkeypatch both).
         - tests/unit/test_daemon_main.py: in single-user mode with
           PRIVACYFENCE_PEEK_AUTO_PROVISION=true, PRIVACYFENCE_PEEK_BASE_URL=http://localhost:7700 and
           PRIVACYFENCE_PEEK_PROVISIONING_KEY set, startup calls peek_provisioning.notify_sign_in with
           the single-user principal; without the variables it is not called with an active
           provisioner. Follow the existing provisioning-start tests in that file.
         - tests/unit/test_peek_provisioning.py and tests/unit/test_grist_provisioning.py: a request for
           Principal(id="single-user", email="admin@localhost", ...) with email_verified=True is queued,
           and provisioning writes users/single-user/credentials/peek_token.json
           (grist: grist_token.json) with the fake client.
      4. CHANGELOG.md, under "## [Unreleased]" "### Added":
         "- **Peek and Grist on a single-user install.** With the same variables, a single-user install on `localhost` connects Peek and Grist for its one person, `admin@localhost`, at start and at each sign-in."
      5. Run `ruff check .` and `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/web/test_single_user_auth.py tests/unit/test_peek_provisioning.py tests/unit/test_grist_provisioning.py -q passes
      - grep -n "notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True)" src/privacyfence/daemon_main.py finds two lines
      - grep -c "notify_sign_in" src/privacyfence/web/single_user_auth.py prints 2 or more

  - id: p3-retire
    title: Reference docs, ADRs 0271 and 0272, delete the plan
    depends_on: [p2-single-user]
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
      1. docs/peek-setup.md, "Automatic accounts (organization mode)", step 3: add that
         `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` does the same from the server's environment. The
         Admin row then shows it as set by the server, and the switch and Offer are locked. Rename the
         section to "Automatic accounts" and add a paragraph "On a single-user install" that describes
         p2 (admin@localhost, connected at start and at each sign-in, the Peek and Grist install
         admins need other addresses). docs/grist-setup.md, "Automatic API keys (organization mode)":
         the same two changes, with the Grist names.
      2. docs/configuration-reference.md, environment variables table: add both variables with the
         Design's values table wording. docs/org-mode-docker.md, "Environment variables" table and
         Quick start step 9: add both variables. deploy/docker/privacyfence.env.example, in the
         "Peek and Grist (optional)" block: add commented
         `# PRIVACYFENCE_PEEK_AUTO_PROVISION=true` and `# PRIVACYFENCE_GRIST_AUTO_PROVISION=true`
         lines with a one-line comment each.
      3. Write docs/adr/0271-automatic-accounts-can-be-switched-on-from-the-environment.md and
         docs/adr/0272-a-single-user-install-provisions-its-one-person-as-verified.md from the plan's
         Design and "Rejected" sections, in the template in docs/adr/README.md, Status Accepted.
         Link source files and the PR, never this plan. Add both to the index in docs/adr/README.md.
      4. Delete docs/env-automatic-accounts-plan.md. `grep -rn "env-automatic-accounts-plan" .` must
         find nothing.
    acceptance:
      - test ! -e docs/env-automatic-accounts-plan.md
      - grep -rn "env-automatic-accounts-plan" docs src finds nothing
      - grep -n "0271\|0272" docs/adr/README.md finds both
      - grep -n "PRIVACYFENCE_PEEK_AUTO_PROVISION" docs/configuration-reference.md docs/org-mode-docker.md docs/peek-setup.md deploy/docker/privacyfence.env.example finds each file
```
