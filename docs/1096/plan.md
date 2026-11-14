# Drop the desktop (local) apps for macOS, Windows and Linux

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** PrivacyFence 8 starts only as an organization server or as the Docker image, single-user mode on localhost included. The macOS, Windows and Linux installers, the companion app, the Claude Desktop extension and the download page are gone (`/download/` becomes a short page that points to `/install/`). A person on a desktop install keeps running 7.x or follows "Moving to organization mode". Starting 8 on a desktop data directory, on one that 7.x never prepared, or with `PRIVACYFENCE_MODE=local`, stops with a message that names that guide.

**Scope:** Removes every local-mode code path (privilege separation, control channel, companion, loopback sign-in, file bridge, local web routes, local settings controller), the desktop command-line flags, the packaging and installer files, `mcpb/`, `cloudflare/downloads/`, the download and releases website pages, the Windows and macOS test legs, and the docs for all of it. Visible to agents: tools no longer take `destination_dir` or a plain `local_path` (attachments and uploads take `upload:` references), and `privacyfence_status` loses its `deprecation` and `next_step` fields. Keeps organization mode, single-user mode, the Docker image, the PyPI package, the sync plugin, the relay and the R2 archive of what is still built. It adds eight ADRs (0283-0290). It does not delete the pre-7 data converters, does not cut a release and does not touch the live GitHub ruleset or Cloudflare account.

**Security:** The start-up check (§3.3) refuses data it cannot trust instead of reading it. Files reach connectors only through upload slots, inline bytes and one-time links (§3.5); the path parameters are removed from every tool. Deleting privilege separation removes no organization-mode protection: org behaviour that was gated on it is pinned to its current org value.

**Review focus:** the order of the code phases and why a top-down cut stays green, including the pinning of privilege-separation-gated behaviour to its organization value (§3.2); the start-up data check and its refusal texts (§3.3); the tool-schema break for `destination_dir`, `local_path` and attachment paths (§3.5); the CI and release ordering, including the branch-protection wedge (§3.6).

## 1. Goal

Desktop (local) mode has been deprecated since 7.0 (ADR 0261, issue #1096 asks to finish it). This change removes it for all three platforms in PrivacyFence 8.0: no installer, no companion app, no local-mode code. One product remains, organization mode, which also covers one person on one computer through the Docker image and single-user mode. The owner answered four scope questions on the board: full removal as 8.0 (`full-8`), include only the start-up check of ADR 0262 (`check-only`), retire the download page and Worker (`retire`), drop the Windows and macOS CI legs (`drop`). Tracking issue: https://github.com/privacyfence/privacyfence/issues/1096.

## 2. Current state

- **Mode default.** `org_mode.DEFAULT_MODE = "local"` (`src/privacyfence/org_mode.py:36`); `resolve_mode` (`:94`) returns the bundle's `mode`, else `PRIVACYFENCE_MODE`, else `"local"`. The Docker image sets `PRIVACYFENCE_MODE=org` (`deploy/docker/Dockerfile:20`). Single-user mode is `org_mode.is_single_user` (`:146`): org mode with no identity provider, localhost only, signed in through `web/single_user_auth.py`.
- **Org never runs local code.** `daemon_main._maybe_start_web_server` (`:917`) returns `_start_org_web_server` (`:999-1007`) before any local code. Org pages do reuse local UI modules: `settings_window_html.build_html(mode="org")`, `approval_window_html`, `approval_list_html`, `dialog_window_html`, `web_prompt`, `web_approval_ui`, `web_shell`, and the module-level helpers of `settings_controller.py` (`:99-636`). Those are stripped, not deleted.
- **Local-only modules** (no org importer): `companion.py` (1033 lines), `daemon_status.py`, `service_control.py`, `windows_service.py`, `windows_acl.py`, `std_streams.py`, `privilege_separation.py` (2267), `web/control_channel.py` (2512), `oauth_loopback.py`, `src/_daemon_entry.py`, `src/_companion_entry.py`, `src/_launcher_entry.py`. The local `LocalSessionStore` and `BootstrapStore` in `web/session_auth.py` go; `check_csrf`, `check_origin` and `human_session_required_json` stay (org routes use them, `routes_security.py:139`).
- **Files.** In org mode only `upload:<id>` capability slots work for most connectors; the local branches (`download_mode != "org"`, the `X-PrivacyFence-File-Bridge` handshake, `LocalFilesNeeded`, direct disk writes) serve the desktop shim. `download_mode` is plumbed through drive, gmail, salesforce, jira, assets, grist, peek, confluence, `gmail_client.py:906` and `daemon_main.py:1695-2032`.
- **Packaging.** Three PyInstaller specs, `installer/`, `debian/`, `com.privacyfence.app.plist`, `privacyfence.service`, `resources/linux/`, the build scripts, `mcpb/` (about 7.7k lines), `cloudflare/downloads/`, the three `*-graphical-session.yml` workflows, and in `build.yml` the jobs `build` (macOS), `build-windows` and `build-deb`.
- **CI that survives.** `build-org-image`, `sbom`, `dedupe`, `finalize-release` (trimmed), `release.yml`, `publish-pypi.yml`, `publish-org-image.yml`, `pages.yml`, `tests.yml` jobs `test`, `test-python-compat`, `org-mode-smoke`, `org-docker-image`, `static-analysis`, `sync`.
- **Tests.** About 35 test files (about 27,000 lines) are local-only and are deleted; about 52 more reference local mode and are stripped. `tests/conftest.py` imports `settings_controller`, `control_channel` and `privilege_separation` for resets, so every deleting phase edits it.
- **ADRs.** The last is 0282; 0261 (local deprecated in 7.x, removed in 8.x) and 0262 (7.x is the migration checkpoint) govern this work. A superseded ADR's body is frozen; only its Status section changes (`docs/adr/README.md`).

## 3. Design

### 3.1 Decisions already made (owner, on the board)

| Question | Answer | Effect |
|---|---|---|
| depth | `full-8` | All local-mode code goes; the daemon is organization-only. |
| checkpoint | `check-only` | §3.3 ships. The pre-7 converters (version-1 clean-up, version-2 to 3 rule conversion, token conversion, legacy verb aliases, bundle import) stay for a separate plan. |
| downloads | `retire` | Download page, releases page and Worker go; old installers stay on their GitHub Releases. |
| platform-ci | `drop` | `platform-windows`, `platform-macos`, `tests/platform`, the Windows coverage floor go. |

### 3.2 Order of the code phases

Top-down cut (rejected: bottom-up, §3.9). Flip the default mode and cut the entry points first; delete code only once nothing reaches it, together with its tests. Every phase leaves `ruff check .`, `python3 -m pytest tests/unit -q` and `python3 -m mypy src/privacyfence` green, because code that is still present is still tested until its own phase deletes both. The order is forced by imports:

1. Orphans with no importer go first (`companion`, `daemon_status`, `service_control`) with the packaged and graphical-session tests.
2. `settings_state.py` is extracted from `settings_controller.py` before anything deletes the class.
3. The mode flips and `daemon_main`'s local branch goes in one phase, because a changed default breaks every `run_app` test that assumes local.
4. The one-shot CLIs, then the local `WebServer` branch, then the local route factories.
5. `settings_controller`, `control_channel` and the local session stores are deleted only after every route factory that imports them is gone; `oauth_loopback`'s `control_channel` import is cut in that same phase.
6. The `authorize_interactive` loopback functions go after the controller and channel that call them.
7. The file bridge and `download_mode` plumbing go before `privilege_separation`, because `local_files.py` imports it.
8. `privilege_separation.py` and `windows_acl.py` are deleted after their five surviving call sites (`paths.py`, `secure_files.py`, `step_up_config.py`, `audit_log.py`, `local_files.py`) are pinned to their org values.
9. `resolve_mode`, `LOCAL_MODE_DEPRECATION` and the `mode=` parameters go last.

Org behaviour that is gated on privilege separation (`step_up_config.py:211,412`, `local_files.can_access_user_files`) is replaced by the org value, `False`, never by deleting a branch body blindly.

Tests that start a real local `WebServer` (`test_mcp_daemon_contract`, `ai_client_harness`, `test_deferred_approval_round_trip`, `test_pending_approval_releases_nothing`, `test_claude_code_contract`, `test_browser_smoke`) are invisible to `verify.fast`. Phase `p01` ports them to a single-user org server first.

### 3.3 The start-up data check (ADR 0284, amends ADR 0262)

New module-level names in `src/privacyfence/data_format.py`:

- `MINIMUM_FORMAT = 7`. `DATA_FORMAT` stays 7 (a downgrade to 7.x keeps working).
- `DESKTOP_TRACES = ("web_base_url", "mcp_token")`, looked for at the data root and under `authority/`.
- `PERSON_DATA = ("users", "org", "credentials", "authority")`.
- `check_upgrade_source(root: Path) -> None`, raising `org_mode.ConfigurationError`.

It replaces the `check_not_newer(read_marker())` call in `daemon_main.main()`, at the same place, before every one-shot command and before `load_config`:

```python
try:
    data_format.check_upgrade_source(paths.data_dir())
except org_mode.ConfigurationError as exc:
    return _refuse_to_start(f"Configuration error: {exc}")
```

Order of checks inside `check_upgrade_source`:

1. Run the existing `check_not_newer` first, on the marker read from `root`. `read_marker` and `marker_path` gain an optional `root: Path | None = None` argument; `None` keeps today's meaning, `paths.data_dir()`.
2. If any `DESKTOP_TRACES` entry exists and `org/org_bundle.json` does not contain `"mode": "org"`, raise the **desktop text**. This applies with or without a marker (7.x local installs create an admin config and can carry a marker). The bundle exception keeps an organization server that was converted in place on 6.x working.
3. Else, if the marker is missing or its `format` is below `MINIMUM_FORMAT`: if any `PERSON_DATA` entry exists, raise the **checkpoint text**; if none exists the directory is fresh (a new Docker volume, or a single-user directory whose `users/single-user/` does not exist yet), so write the marker at once with `atomic_write_json` as `{"format": 7, "written_by": __version__, "completed_at": <UTC ISO>}`. Writing at first start avoids refusing a fresh single-user install that is restarted before setup finished (the marker was written only after an admin config existed).

`{root}` below is the data directory path.

Desktop text, exactly:

> This data directory ({root}) belongs to a PrivacyFence desktop install. PrivacyFence 8 runs only as an organization server or the Docker image and does not read desktop data. Keep using PrivacyFence 7 on this computer, or move to organization mode: https://privacyfence.eu/docs/moving-to-organization-mode/

Checkpoint text, exactly:

> This data directory ({root}) has not been prepared for PrivacyFence 8. Install the latest PrivacyFence 7, start it once, check that the Admin page says "Ready for PrivacyFence 8: yes", then start this release again: https://privacyfence.eu/docs/moving-to-organization-mode/

No URL anchor (the docs site serves the 7.x copy of the page until `v8.0.0` is tagged, §3.7). A unit test asserts `docs/moving-to-organization-mode.md` exists. Tests pass `tmp_path` as `root`; tests that call `main()` must patch `paths.data_dir` so a developer's checkout with an old `authority/` cannot flake them. `migrate_everything` and the "Not ready for PrivacyFence 8" logging stay unchanged.

### 3.4 What `mode` means in 8.0 (ADR 0285)

- No mode set: organization mode (`DEFAULT_MODE = "org"`).
- `PRIVACYFENCE_MODE=org` and a bundle `"mode": "org"` are accepted silently (the Dockerfile and existing compose files set it).
- `local`, from either source, raises `ConfigurationError` through `org_mode.refuse_local_mode(source: str, value: str)`, text exactly: `{source} says "local": desktop mode is not part of PrivacyFence 8. Remove it (organization mode is the only mode), or keep using PrivacyFence 7 for a desktop install: https://privacyfence.eu/docs/moving-to-organization-mode/` where `{source}` is `PRIVACYFENCE_MODE` or `org_bundle.json's "mode"`.
- Any other value: `ConfigurationError` `{source} must be "org".`
- A bundle and the variable that disagree are still refused, as today.
- `refuse_local_mode` ships in the mode-flip phase inside `resolve_mode`. Phase `p14b` replaces `resolve_mode` by `validate_mode(org_config: dict) -> None` (same checks, no return value, called once when the bundle is loaded and once in `run_app`), deletes `Mode`, `DEFAULT_MODE` and `LOCAL_MODE_DEPRECATION`, and makes `is_single_user` return `not _idp_present(org_config)`; phase `p14c` removes the `mode=` parameters.
- `personal_settings.py` keeps importing documents whose `source.mode` is `"local"` (`:228-234`): that is the documented desktop-to-org move.

### 3.5 Files and tools (ADR 0286)

Files reach the server only through the upload slot (`privacyfence_create_upload_slot`, `PUT /mcp-files/slots/{slot}`), inline bytes and the one-time link of ADR 0160. Remove:

- `local_files.py`: `LocalFilesNeeded`, `META_KEY`, `NO_BRIDGE_UPLOAD_MESSAGE` (rewritten, see below), `build_need_uploads_files`, `force_bridge_for_tests`, `_deliver_direct`, `_deliver_bridge`, the `privilege_separation` import. Connectors still call `can_access_user_files` and `deliver_file` until the connector phases; phase `p11` makes `can_access_user_files` return `False` unconditionally and `deliver_file` keep only its link branch, and the connector phases delete both names with their call sites.
- `local_files.py` keeps: `LocalFileAccessError`, `UPLOAD_REF_PREFIX`, `call_context`, `_claim_upload`, `commit_uploads`, `build_upload_slot`, `read_local_file` and `local_file_size` (upload branch only), `check_delivery_size`, `deliver_export` (org branch), `configure_file_bridge`, `call_produced_deliveries`. `configure_file_bridge()` and the `file_bridge.max_download_bytes` key stay because org exports use them (`daemon_main.py:993`).
- `routes_file_bridge.py`: the bearer router, `build_file_bridge_asgi_app`, `mount_file_bridge`, the `/mcp-files/uploads` and `/mcp-files/downloads` handlers. Keep `mount_capability_routes`, `/mcp-files/slots/{slot}` and `/mcp-files/fetch/{token}`.
- `routes_mcp.py`: the `X-PrivacyFence-File-Bridge` header and the `_meta` need_uploads and deliver handshake (`:291-310`, `:505-567`).
- Every tool schema loses `destination_dir` and plain `local_path`; `attachments` and `upload`-style parameters accept `upload:<id>` references only. Every connector's `download_mode` attribute, parameter and branch goes (drive, gmail, salesforce, jira, assets, grist, peek, confluence, `gmail_client.py`, `drive_client.py`, `daemon_main.build_connectors`).
- `NO_BRIDGE_UPLOAD_MESSAGE` becomes: `This tool takes a file through an upload slot. Call privacyfence_create_upload_slot, send the file to the returned address, and pass the returned upload: reference.`
- Tool descriptions are rewritten to say that (ADR 0115 prose guidance) and `docs/tools-reference.md` is regenerated with `python3 scripts/generate_tools_reference.py`. Connector phases also give `local_files` functions that take `download_mode` a default of `"org"` first, so callers can drop the argument one connector at a time; the last connector phase deletes the parameter.
- **Undeclared arguments.** Agents with a cached schema may still send `destination_dir` or `local_path`. The dispatcher drops them, so a stale argument is ignored instead of raising `TypeError`. The filter lives at the start of `McpDispatcher.call` (`web/mcp_dispatch.py:255`): drop every key of `args` that is neither the name of a `ToolParam` of the tool's `ToolSpec` (found through `connector.tool_specs()`, as `web/mcp_dispatch.py:380` does) nor in `_CONNECTOR_COMPUTED_ARGS` (`:56`). Phase `p11` adds it, with the test first.
- **`staged_download`.** Nothing sets the dedupe-replay flag `staged_download` (`mcp_dispatch.py:340`) once the bridge is gone, and org staged links never set it. Keep the guard; the phase that removes the bridge makes the org staging paths set it, with a test that a repeated call does not mint a second link.
- `privacyfence_status`: remove the `deprecation` field and `next_step: open_privacyfence_companion` (`mcp_dispatch.py:704,729-739`, `mcp_tools.py:264-267`). `SERVER_INSTRUCTIONS` (`routes_mcp.py:96`) stops telling the model to open the companion app and says: `If PrivacyFence needs the person to sign in or connect a service, tell them to open the PrivacyFence server's Connect page, or to ask their administrator.` Both are breaking changes for clients that read them; the changelog says so.

### 3.6 Release pipeline and CI (ADR 0288, 0289)

- `build.yml` keeps `dedupe`, `build-org-image`, `sbom`, `finalize-release`. It deletes `build`, `build-windows`, `build-deb`. The step "Run org-mode Ubuntu release smoke test" (`build.yml:650`, in `build-deb`) moves into `build-org-image`. The step "Publish release artifacts to Cloudflare R2" of `build` also uploads `scripts/build_org_bundle.py`, `scripts/sync_room_directory.py`, `scripts/sign_connector_config.py` and the connector-config page; of these only `build_org_bundle.py` (desktop-only, refuses `--mode org`) is deleted, the others move to `finalize-release`.
- `finalize-release`: `needs: [sbom, build-org-image]`; drop `actions: read`; delete the graphical-coverage step, the installer globs of "Attach release assets" (`PrivacyFence-*.dmg`, `PrivacyFence.mcpb`, `PrivacyFence-*-setup.exe`, `privacyfence_*.deb`, `sbom-shim-npm.cdx.json`), "Publish release manifest and promote to latest", and "Smoke-test the published download routes (HEAD)". Keep the sync plugin zip, the connector-config page, the pre-release notes step and, last, "Point the `stable` branch" (ADR 0169).
- `scripts/r2_release.py` keeps `channel`, `check-tag`, `upload`, `sha256_file`, `_r2_client` and the immutability guard; it deletes `_INSTALLERS`, `REQUIRED_ARTIFACT_IDS`, `classify_installer`, `build_manifest`, `verify`, `promote`, `finalize`. `scripts/release_stats.py` imports `classify_installer` and is deleted earlier, with the website.
- Delete `publish-mcp-registry.yml`, `scripts/mcp_registry_server_json.py` and its test, `mcpb/server.json.tmpl` (with `mcpb/`). The registry's already-published entry is not touched (manual step).
- Delete `deploy-download-worker.yml` and `cloudflare/downloads/`. `cloudflare/oauth-relay/` stays.
- `tests.yml`: delete `platform-windows`, `platform-macos`, the shim steps of `test` and `PRIVACYFENCE_REQUIRE_NODE`; drop the `--ignore` of `test_shim_mcp_contract.py` from `test-python-compat`. The `ai_client` `npm ci` step stays.
- `scripts/update_branch_protection.py:106-107` lists `platform-windows` and `platform-macos` as required checks on the live ruleset. The phase that edits `tests.yml` also edits that list and `tests/unit/test_update_branch_protection.py:359`; the owner applies the ruleset before merging (manual step `ma1`). Applying it earlier would drop Windows and macOS gating on `main` while 7.x still ships.
- `.tld/toolkit.yaml`: delete the three graphical-session entries of `ci.dispatchable`, reword the `build.yml` entry to "the org image, SBOM and release assets", delete the two `mcpb/shim` `npm` lines of `verify.dod`, and the `dod_conditional` entries for `mcpb/shim/src/**` and `cloudflare/downloads/**`. `.claude/hooks/session-start.sh` stops running `cd mcpb/shim && npm ci`. These land in the packaging phase, because they break the moment `mcpb/` is deleted.
- Python packaging: `pystray`, both `Pillow` entries, `pyinstaller`, the `privacyfence-companion` script, the `Download` project URL, the MacOS and Windows and "End Users/Desktop" classifiers go; lockfiles are regenerated with `scripts/update_dependency_locks.sh`. The `packaged` pytest marker stays (Docker and Ubuntu release tests use it); only the `platform` marker is deleted, after `tests/platform/` is gone.
- Coverage floors: `scripts/check_coverage_floor.py` loses `WINDOWS_MODULE_FLOORS`, `--platform` and the floors of deleted modules; `OVERALL_FLOOR` and the floors of shrunken modules are re-measured in the last code phase.

### 3.7 Docs and website

- The docs site (`/docs/`) is built from the newest stable tag (ADR 0052), so deleted docs leave the site only when `v8.0.0` is tagged. Hand-written website pages must not link to a doc 8.0 deletes, and a page that links to a doc 8.0 adds needs `DOCS_PENDING_RELEASE` (`scripts/build_site.py`, ADR 0276).
- Delete: `install-macos.md`, `install-windows.md`, `install-linux.md`, `platform-support.md` (with `tests/unit/test_docs_platform_pages.py` and `test_minimum_os_versions.py`), `packaging.md` (its Docker image, CI and Glama sections move into `org-mode-docker.md` and `releasing.md`).
- `downloads-and-release-kpi.md` also owns the "Release archive (R2)", "Cloudflare resources" and credentials sections that `oauth-relay.md:33,42` and `releasing.md:307-316` link to. It is renamed `release-archive.md`, keeps those sections, loses the download-count, KPI and Worker sections, and every link, `build_site.CONTRIBUTOR_DOCS` and `cloudflare/oauth-relay/worker-configuration.d.ts:2` follow.
- `docs/moving-to-organization-mode.md` stays and becomes "removed in 8.0; how to move"; it is the target of both refusal texts.
- Tests that parse docs must stay green in the docs phases: `test_docs_links`, `test_docs_references_exist` (source comments cite deleted docs, `platform-support.md` and `downloads-and-release-kpi.md` among them), `test_docs_no_history` (bans "no longer", "formerly", "used to", "now"; hard-codes doc names), `test_docs_configuration_reference`, `test_readme_site_links`, `test_website_docs_allowlist`, `test_website_docs_pages`, `test_website_agent_pages`, `test_website_links`.
- Website: `website/download/`, `website/releases/` and the download counter of `website/stats.js` go. `website/download/` is replaced by a stub page that links to `/install/` and to the GitHub Releases page (so old links do not 404). ADR 0277's call to action already points at `/install/`; `/download/` redirect text is amended by ADR 0287. `scripts/site_security_headers.py:34` drops `https://downloads.privacyfence.eu` from `connect-src`. `src/privacyfence/catalog_server.py:26,33` point at `/install/`. `tests/website_site.py` and `tests/unit/test_build_site.py` are edited, not deleted (about 30 test modules import `website_site`).
- Agent pages: `website/ai-agents/claude-desktop` and `chatgpt` describe the extension and desktop mode; they are rewritten to the custom-connector and Docker path. Every `_data/clients.json` entry keeps a page.
- Sync: `sync/README.md` and its byte-identical copy `plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md` lose the "Local mode" section and companion mentions and are changed together. `DEFAULT_SERVER = "http://127.0.0.1:8765/mcp"` stays: the single-user host allowlist accepts `127.0.0.1` (`web/server.py:1084`).

### 3.8 ADRs

Next free number is 0283. Written first (phase `p00`) so code comments can cite them; the retirement phase adds the Status lines to the old ADRs and the changelog.

| ADR | Decision | Relations |
|---|---|---|
| 0283 | Desktop mode, its installers, the companion and the `.mcpb` extension are removed; organization mode, including single-user, is the only mode, shipped as the Docker image and the PyPI package. Rejected: keeping pip local mode; shipping a `.deb` that runs org mode. | Supersedes 0002, 0003, 0261. |
| 0284 | 8.x refuses a data directory without the 7.x marker or with desktop traces, and marks a fresh directory at first start. | Amends 0262. |
| 0285 | `mode` `local` refuses to start, `org` is accepted, no mode means org. | Relates to 0260. |
| 0286 | Files move only by upload slot, inline bytes and one-time link; the path parameters are removed from every tool. | Supersedes 0007, 0012; amends 0028, 0160. |
| 0287 | The download page and the Worker are retired; old installers stay on GitHub Releases; `/download/` points to `/install/`. | Supersedes 0024; amends 0277. |
| 0288 | Releases build the org image, the Python package and the SBOM; R2 keeps uploads without a manifest or `latest`. | Supersedes 0057; amends 0087. |
| 0289 | Linux is the only CI platform; the Windows and macOS legs and floors are dropped. | Relates to 0057. |
| 0290 | The MCP registry listing of the `.mcpb` extension is no longer published and its workflow is removed; the published entry is left as it is. | Supersedes 0112. |

Old ADRs that get a Status line (body frozen): `Superseded by [ADR 0283]` for 0002, 0003, 0261; `Superseded by [ADR 0286]` for 0007; `Deprecated: ADR 0283 removed desktop mode` for 0012, 0018, 0026, 0027, 0029, 0031, 0039, 0041, 0042, 0043, 0044, 0045, 0046, 0054, 0058, 0059, 0060, 0062, 0088, 0104, 0183, 0184, 0187, 0203; `Superseded by [ADR 0287]` for 0024; `Superseded by [ADR 0288]` for 0057; `Superseded by [ADR 0290]` for 0112; `Amended by [ADR 0284]` for 0262; `Amended by [ADR 0286]` for 0028 and 0160; `Amended by [ADR 0287]` for 0277; `Amended by [ADR 0288]` for 0087. The retirement phase greps `docs/adr` for `companion`, `mcpb`, `privilege`, `installer` and `DMG` for any ADR this list missed.

### 3.9 Rejected alternatives

- **Packaging only in 7.x** (owner chose full removal): leaves two products' code in one tree.
- **Bottom-up deletion** (leaves first, mode flip last): every phase would have to keep the live local path working with half its machinery gone, and the default would be a crippled mode for most of the run.
- **Keeping `local` as a recognised mode name that exits with a message from `resolve_mode`**: needs `resolve_mode` and every `mode=` parameter to live on; §3.4 refuses at configuration load and deletes the plumbing.
- **Bumping `DATA_FORMAT` to 8**: would stop 7.x from reading data 8.0 wrote, making the migration irreversible; left to the converters plan.
- **Keeping the Worker and a legacy download page**: owner chose `retire`.

## 4. ADRs

- 0283 desktop mode, installers, companion and extension removed (§3.8).
- 0284 8.x start-up data check (§3.3).
- 0285 `mode` accepts only `org` (§3.4).
- 0286 files only by upload slot, inline bytes or one-time link (§3.5).
- 0287 download page and Worker retired.
- 0288 release pipeline builds the org image, package and SBOM only (§3.6).
- 0289 Linux is the only CI platform (§3.6).
- 0290 the MCP registry listing of the extension is no longer published (§3.6).

## 5. Manual steps

### Before implementation

**mb1-7x-final (about 10 minutes).**

1. Open https://github.com/privacyfence/privacyfence/pulls and merge or close every pull request you want in 7.x: merging this work makes `main` an 8.x branch.
2. Cut the last 7.x release you want by following "Cutting a release" in https://github.com/privacyfence/privacyfence/blob/feature/drop-desktop-mode/docs/releasing.md, or create a `releases/7.x` branch from the current `main` on https://github.com/privacyfence/privacyfence/branches for later 7.x patches.
3. You should see the release on https://github.com/privacyfence/privacyfence/releases, or the new branch in the list.
4. Done when: no 7.x change you still want is waiting only on `main`, and you know which branch later 7.x patches go to.

### After implementation

**ma1-branch-protection (about 5 minutes).** Before merging the feature pull request:

1. Check out the feature branch locally.
2. Run `GITHUB_TOKEN=<token> python3 scripts/update_branch_protection.py`, with a fine-grained token that has repository **Administration: read and write** for this repository (create it at https://github.com/settings/personal-access-tokens/new; delete it afterwards). The script drops `platform-windows` and `platform-macos` from the required checks of `main`.
3. If a `releases/*` branch exists, run it again with `--branch "releases/**"`.
4. Open https://github.com/privacyfence/privacyfence/rules and confirm neither check is required any more.
5. Report back: pass or fail, and the check names now required.

**ma2-retire-worker (about 15 minutes).** After the pull request merges:

1. In the Cloudflare dashboard open Workers & Pages (https://dash.cloudflare.com/?to=/:account/workers-and-pages), open the download Worker, delete its route on `downloads.privacyfence.eu` and delete the Worker.
2. Open D1 (https://dash.cloudflare.com/?to=/:account/workers/d1) and delete the download-counts database.
3. Open https://github.com/privacyfence/privacyfence/settings/environments, delete the `downloads-worker` environment and its secrets (`CF_DOWNLOADS_WORKER_*`).
4. Report back: pass or fail for each of the three.

**ma3-registry-listing (about 5 minutes).**

1. Open https://registry.modelcontextprotocol.io/ and search for `privacyfence`; you should see the 7.x listing of the `.mcpb`.
2. The registry's own documentation (https://github.com/modelcontextprotocol/registry) says how to deprecate or remove a listing. Leave it or delist it.
3. Report back: left as is, or delisted.

**ma4-merge-timing (about 5 minutes).**

1. Confirm ma1 is done and the feature pull request's checks are green.
2. Merge the pull request only when you are ready to start the release workflow (https://github.com/privacyfence/privacyfence/actions/workflows/release.yml) for 8.0.0a1 or 8.0.0, because `pages.yml` deploys the website from `main` at once while `/docs/` keeps serving the 7.x docs until the tag.
3. Report back: the merge time and the tag you cut.

The live QA report of the touched connectors is not manual: phase `p12d` dispatches `qa-record-fixture.yml` for each of them.

## 6. Risks and open questions

- **`data_dir()` resolution.** Any change to it beyond removing the separated-root override moves an existing pip organization install to an empty directory, which the start-up check then treats as fresh. Tell: a diff to `data_dir()` in a phase brief that does not name it. Stop with `status=blocked`.
- **Org servers converted in place carry desktop traces.** `mcp_token` and `web_base_url` are written only by `control_channel` and `PerUserTokenVerifier`, which this plan deletes; organization mode does not write them (`mcp_url` it does write, so it is not a trace). A worker confirms by grepping the org code paths before deleting the writers; if org writes either file, stop with `status=blocked`.
- **Integration harness cannot get a full-scope bearer.** Scoped tokens are rule-limited; tool listings shrink in the contract tests. Tell: `len(tools)` differs. Mint through `OrgOAuthProvider` in-process instead of a scoped token.
- **Tool-schema break.** Agents with a cached schema send removed arguments; §3.5's dispatcher filter handles it, and the first removing phase tests it.
- **Whole-tree gates outside `fast`.** `mypy src/privacyfence`, `tests/integration`, the doc tests and the coverage floor fail without showing in `verify.fast`; each phase's acceptance names them.
- **Coverage floor.** Stripped modules that keep a 100% floor (`session_auth`, `secure_files`, `principal`) must keep it; the shrunken modules' floors are re-measured last (`p22`).
- **7.x clients.** The 7.x desktop update checker (GitHub releases/latest) will offer 8.0, which has no installer. Not fixed here; the last 7.x release (`mb1`) is the place.
- **Lockfile regeneration needs `uv` and the package index.** If the phase `p17` worker cannot reach it, it stops with `status=blocked`; the owner then runs `scripts/update_dependency_locks.sh`.
- **A phase brief names a symbol or line that does not exist.** Line numbers are from commit `9eb7b4c1`. Where a brief says `file:line`, grep for the symbol first; if the symbol is absent or the surrounding code is clearly different from what the brief describes, stop with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: drop-desktop-mode
feature_branch: feature/drop-desktop-mode
tracking_issue: 1096
max_parallel: 2
manual_before:
  - id: mb1-7x-final
    title: Ship what 7.x still needs before main becomes 8.x
    why: Merging this work makes main an 8.x branch; 7.x fixes waiting on main would have to ship from a branch.
    done_when: Every 7.x change you want is released or on a releases/7.x branch, and you know where later 7.x patches go.
manual_after:
  - id: ma1-branch-protection
    title: Remove platform-windows and platform-macos from the required checks of main
    why: The live ruleset requires both; the feature pull request never reports them, so it cannot merge.
  - id: ma2-retire-worker
    title: Delete the download Worker, its route, its D1 database and its secrets
    why: Nothing in the repository can reach the Cloudflare account or the repository's environments.
  - id: ma3-registry-listing
    title: Decide whether to delist the 7.x .mcpb from the MCP registry
    why: The registry entry points at a 7.x release asset and only the registry's own tooling can remove it.
  - id: ma4-merge-timing
    title: Merge only when ready to tag 8.0.0 or 8.0.0a1
    why: pages.yml deploys the website from main at once while /docs/ keeps serving the 7.x tag.
verify_after_merge:
  - python3 -m mypy src/privacyfence
  - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q
final_checks:
  - "`tld plan deviations check` on the deviations file implement keeps next to this plan, with `--plan docs/1096/plan.md --all`, exits 0"
  - "ADRs 0283, 0284, 0285, 0286, 0287, 0288, 0289 and 0290 exist in docs/adr, are Accepted, and are listed in docs/adr/README.md"
  - "CHANGELOG.md has the new entries under ## [Unreleased] and no new version heading"
  - "`grep -rnE '^\\s*(from|import)\\s.*(companion|privilege_separation|control_channel|oauth_loopback|settings_controller|std_streams|windows_acl|windows_service|update_checker)' src tests scripts --include=*.py` prints nothing"
  - "`grep -rn 'resolve_mode\\|LOCAL_MODE_DEPRECATION\\|download_mode' src` prints nothing"
phases:
  - id: p00-adrs-new
    title: Write ADRs 0283-0290 and their index rows
    depends_on: []
    complexity: M
    touches:
      - docs/adr/0283-*.md
      - docs/adr/0284-*.md
      - docs/adr/0285-*.md
      - docs/adr/0286-*.md
      - docs/adr/0287-*.md
      - docs/adr/0288-*.md
      - docs/adr/0289-*.md
      - docs/adr/0290-*.md
      - docs/adr/README.md
    brief: |
      Read first: docs/1096/plan.md sections 3.1 to 3.9; docs/adr/README.md (template and the index row format); docs/adr/0261-local-mode-is-deprecated-in-7-and-removed-in-8.md as the shape to copy.
      1. Create these files in docs/adr/, each in the shape of ADR 0261 (Status, Context, Decision, Alternatives considered, Consequences, Verification, Related), Status `Accepted — <today's date from date +%F>.` plus the relation in the table of plan section 3.8:
         adr/0283-desktop-mode-and-its-installers-are-removed-in-8.md
         adr/0284-8x-refuses-a-data-directory-that-is-not-ready-for-it.md
         adr/0285-the-mode-setting-accepts-only-org.md
         adr/0286-files-move-by-upload-slot-inline-bytes-or-one-time-link-only.md
         adr/0287-the-download-page-and-worker-are-retired.md
         adr/0288-releases-build-the-org-image-the-package-and-the-sbom-only.md
         adr/0289-linux-is-the-only-ci-platform.md
         adr/0290-the-mcp-registry-listing-of-the-extension-is-no-longer-published.md
      2. Content comes only from plan sections 3.1 to 3.9: the one-line decision from the table in 3.8, the alternatives from 3.9 and from the text of the matching section (3.3 for 0284, 3.4 for 0285, 3.5 for 0286, 3.6 for 0288, 0289 and 0290, 3.7 for 0287), the exact refusal texts verbatim in 0284 and 0285. Do not cite the plan, a phase id or issue number in an ADR. Verification sections name the tests the phases add: 0284 `tests/unit/test_data_format.py`, 0285 `tests/unit/test_org_mode.py`, 0286 `tests/unit/test_local_files.py` and `tests/unit/web/test_routes_file_bridge.py`, 0287 `tests/unit/test_build_site.py`, 0288 `tests/unit/test_release_assets.py` and `tests/unit/test_r2_release.py`, 0289 `tests/unit/test_update_branch_protection.py`, 0290 no test (its subject is a deleted workflow file; say so in one sentence). These Verification sections are final: later phases never edit an accepted ADR's body.
      3. Add one index row per ADR to docs/adr/README.md in the same format as the rows of 0278-0282, with the Status column text `Accepted; supersedes 0002, 0003, 0261` (and the matching text for each ADR from the table in 3.8).
      4. Do not edit any old ADR (the retirement phase does).
      5. Run: python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q.
      Stop with status=blocked if a docs/adr file numbered 0283 to 0290 already exists.
    acceptance:
      - ls docs/adr/0283-*.md docs/adr/0284-*.md docs/adr/0285-*.md docs/adr/0286-*.md docs/adr/0287-*.md docs/adr/0288-*.md docs/adr/0289-*.md docs/adr/0290-*.md lists eight files
      - grep -cE "^\| \[(028[3-9]|0290)\]" docs/adr/README.md prints 8
      - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q passes

  - id: p01-org-test-harness
    title: Port the daemon-level integration tests to a single-user org server
    depends_on: []
    complexity: M
    touches:
      - tests/integration/org_server_harness.py
      - tests/integration/test_mcp_daemon_contract.py
      - tests/integration/ai_client_harness.py
      - tests/integration/test_deferred_approval_round_trip.py
      - tests/integration/test_pending_approval_releases_nothing.py
      - tests/integration/test_claude_code_contract.py
      - tests/integration/test_browser_smoke.py
    brief: |
      Read first: docs/1096/plan.md sections 3.2 and 6; docs/testing-policy.md (layers 3 to 5); tests/integration/test_mcp_daemon_contract.py; tests/integration/ai_client_harness.py; tests/integration/test_org_mode_smoke.py (how a single-user org WebServer is started); src/privacyfence/web/server.py (the org branch of WebServer).
      Goal: after this phase none of the files in `touches` builds a local-mode WebServer (one created with `mcp_token=` and no `org=`), so the later phases can delete local mode without breaking them. Tests only; no source file changes.
      1. Create tests/integration/org_server_harness.py: a context manager `single_user_org_server(tmp_path)` that starts an org-mode WebServer as single-user (no identity provider, host localhost, data directory under tmp_path) on a free port, and returns an object with `.base_url`, `.mcp_url` and `.bearer(scope="full")`. `bearer` mints an access token in-process through the org OAuth provider (`OrgOAuthProvider`, defined in src/privacyfence/web/oauth_provider.py) with every tool visible, not a scoped token. Copy the startup code from the org tests named above; do not start a daemon process.
      2. Port each of the six test files to the harness: replace the local WebServer fixture and the `mcp_token` bearer with `single_user_org_server` and `.bearer()`. Keep every assertion about tool behaviour; delete only assertions about the local-only handshake (`X-PrivacyFence-File-Bridge`, `_meta` need_uploads, `mcp_url` file discovery from the local data directory) and say which in a one-line comment-free commit message.
      3. In tests/integration/test_browser_smoke.py replace the two local WebServer fixtures (around the lines that build `WebServer(... mcp_token=...)`) by the harness; tests that exercise local-only pages (companion strip, desktop bootstrap) are deleted in the later phase that deletes the page, so here skip nothing and delete nothing else.
      4. Run: python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_deferred_approval_round_trip.py tests/integration/test_pending_approval_releases_nothing.py tests/integration/test_claude_code_contract.py -q and python3 -m pytest tests/integration/test_browser_smoke.py -q (browser tests may report SKIPPED here when Chromium cannot launch; say so in your report and do not count it as a pass).
      Stop with status=blocked if the org provider cannot mint a token that lists every tool (the tool count of a full token differs from the old local-token count): report both counts.
    acceptance:
      - grep -rn "mcp_token=" tests/integration/test_mcp_daemon_contract.py tests/integration/ai_client_harness.py tests/integration/test_deferred_approval_round_trip.py tests/integration/test_pending_approval_releases_nothing.py tests/integration/test_claude_code_contract.py tests/integration/test_browser_smoke.py prints nothing
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_deferred_approval_round_trip.py tests/integration/test_pending_approval_releases_nothing.py tests/integration/test_claude_code_contract.py -q passes
      - python3 -m pytest tests/unit -q passes

  - id: p02-orphans
    title: Delete the companion and the packaged and graphical-session tests
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/companion.py
      - src/privacyfence/daemon_status.py
      - src/privacyfence/service_control.py
      - src/_companion_entry.py
      - src/_launcher_entry.py
      - tests/unit/test_companion.py
      - tests/unit/test_daemon_status.py
      - tests/unit/test_service_control.py
      - tests/system/test_local_mode_system.py
      - tests/integration/test_linux_graphical_session_autostart.py
      - tests/integration/test_macos_graphical_session_autostart.py
      - tests/integration/test_windows_graphical_session_autostart.py
      - tests/integration/test_macos_packaged_smoke.py
      - tests/integration/test_macos_pkg_install.py
      - tests/integration/test_macos_pkg_smoke.py
      - tests/integration/test_windows_packaged_smoke.py
      - tests/integration/test_deb_packaged_lifecycle.py
      - tests/platform/**
      - tests/integration/test_atomic_write_concurrency.py
      - tests/integration/test_single_instance_lock_cross_process.py
      - tests/windows_task_contract.py
      - tests/packaged_policy_probe.py
      - tests/packaged_step_up.py
      - tests/unit/test_check_graphical_session_coverage.py
      - scripts/check_graphical_session_coverage.py
      - .github/workflows/linux-graphical-session.yml
      - .github/workflows/macos-graphical-session.yml
      - .github/workflows/windows-graphical-session.yml
      - .tld/toolkit.yaml
      - pyproject.toml
      - .github/workflows/build.yml
      - scripts/check_coverage_floor.py
      - tests/unit/test_check_coverage_floor.py
    brief: |
      Read first: docs/1096/plan.md sections 3.2 and 3.6; docs/testing-policy.md (the platform layer).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Delete the modules in `touches` under src/privacyfence (companion.py, daemon_status.py, service_control.py) and src/_companion_entry.py and src/_launcher_entry.py. First run `grep -rn "companion\|daemon_status\|service_control" src tests scripts --include=*.py -l`; every hit outside the deleted files and the tests listed in `touches` must be a comment or docstring only. If any hit is code that stays, stop with status=blocked and list it.
      2. Delete the listed tests, helpers (tests/windows_task_contract.py, tests/packaged_policy_probe.py, tests/packaged_step_up.py) and the three *_graphical_session workflows, scripts/check_graphical_session_coverage.py and its test.
      3. tests/platform/: move test_atomic_write_concurrency.py and test_single_instance_lock_cross_process.py to tests/integration/ (git mv, same names; fix their imports and any path they compute from __file__), then delete everything else under tests/platform/. In both moved files change `pytestmark = pytest.mark.platform` to `pytestmark = pytest.mark.integration` (pyproject uses --strict-markers and step 4 deletes the `platform` marker).
      4. pyproject.toml: delete the `privacyfence-companion = "privacyfence.companion:main"` line from [project.scripts]; delete the `platform` marker from [tool.pytest.ini_options] markers (keep `packaged`). Delete any other mention of tests/platform in pyproject.toml.
      5. .tld/toolkit.yaml: delete the three `ci.dispatchable` entries for linux-graphical-session.yml, windows-graphical-session.yml and macos-graphical-session.yml. Do not edit any other key.
      6. .github/workflows/build.yml: delete the step "Check graphical-session/autostart coverage" of finalize-release together with the comment block above it. Do not touch other steps.
      7. In scripts/check_coverage_floor.py delete the `companion` module key from the floors (about line 212) and its mention in tests/unit/test_check_coverage_floor.py.
      8. Run `grep -rln "graphical.session\|check_graphical" .github scripts tests` and fix each hit that is in a file of `touches`; list any other hit in your final report (the docs phases handle docs).
      9. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence.
      Stop with status=blocked if tests/unit imports a deleted module outside the tests listed in `touches`.
    acceptance:
      - ls src/privacyfence/companion.py src/privacyfence/daemon_status.py src/privacyfence/service_control.py fails with no such file
      - ls tests/platform fails with no such file
      - ls tests/integration/test_atomic_write_concurrency.py tests/integration/test_single_instance_lock_cross_process.py lists both files
      - grep -n 'privacyfence-companion\|"platform:' pyproject.toml prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p03-settings-state
    title: Move the settings helpers out of settings_controller into settings_state
    depends_on: [p02-orphans]
    complexity: M
    touches:
      - src/privacyfence/settings_state.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/server.py
      - src/privacyfence/web/routes_admin.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/org_install_policy.py
      - tests/unit/test_settings_state.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/conftest.py
    brief: |
      Read first: docs/1096/plan.md sections 2 and 3.2; src/privacyfence/settings_controller.py lines 1-640 (the module-level helpers) and its class from line 638 on.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Create src/privacyfence/settings_state.py holding every module-level name of settings_controller.py that sits above `class SettingsController` (the helpers at lines 99-636, plus `set_main_dispatcher` and `call_on_main` with the `_main_dispatch` module variable, `REPO_URL`, `PRIVACY_*_LABELS`, `_pii_general_fields`, `_privacy_state_from_config`, `_about_state_dict`, `_auto_accept_state_from_rows`, `cached_row_value`, `row_value_items`, `sentence_parts`). Move them unchanged; the module docstring says what the module is for in one sentence.
      2. In settings_controller.py replace the moved definitions by `from .settings_state import ...` of the same names so the class and its tests keep working.
      3. Repoint the moved names in the four importers to settings_state: web/server.py (the import at about line 113), web/routes_admin.py (about line 67), web/routes_settings.py (about line 167), web/org_install_policy.py (about line 72). Where one of them also imports the `SettingsController` class, it keeps importing only that name from settings_controller; the later phase that deletes the class removes those imports with the code that uses them. Also repoint tests/unit/web/test_routes_org_settings.py (it imports `_privacy_state_from_config` and `_short_id` from settings_controller at about line 67).
      4. Tests: create tests/unit/test_settings_state.py by moving, not copying, the helper tests out of tests/unit/test_settings_controller.py (every test that exercises a moved name and not the class); change `monkeypatch.setattr` targets and imports in all tests that patch a moved name (grep -rn "settings_controller" tests).
      5. tests/conftest.py: change the `_main_dispatch = None` reset to `settings_state._main_dispatch = None`.
      6. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, and python3 -m pytest tests/unit/test_settings_state.py -q --cov=privacyfence.settings_state --cov-branch --cov-report=term-missing (the coverage of the moved code must be at least what it was in settings_controller).
      Stop with status=blocked if a moved helper reads `SettingsController` or an instance attribute.
    acceptance:
      - ls src/privacyfence/settings_state.py lists the file
      - grep -rn "settings_controller import" src/privacyfence/web | grep -v SettingsController prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p04-mode-flip
    title: Make organization mode the default and cut the local daemon branch
    depends_on: [p03-settings-state]
    complexity: M
    touches:
      - src/privacyfence/org_mode.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/agent_label.py
      - tests/conftest.py
      - tests/unit/test_org_mode.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_agent_label.py
      - tests/unit/web/test_agent_attestation.py
    brief: |
      Read first: docs/1096/plan.md sections 3.2, 3.4 and 6; src/privacyfence/daemon_main.py around _maybe_start_web_server (about line 917), _local_effective_org_config (about 686), run_app (about 2400-2530) and main (about 3100-3235); docs/adr/0260-organization-mode-can-start-from-the-environment-and-run-single-user-on-localhost.md.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. org_mode.py: set `DEFAULT_MODE = "org"`. Add `refuse_local_mode(source_label: str, value: str) -> None` raising `ConfigurationError` with exactly the texts of plan section 3.4 (the `local` text and, for any other value that is not `org`, `{source} must be "org".`) and call it from `resolve_mode` for both sources (variable and bundle). `resolve_mode` returns "org" or raises; it is deleted in a later phase, so keep its signature. Keep `LOCAL_MODE_DEPRECATION` for now.
      2. daemon_main.py: delete the local branch of `_maybe_start_web_server` (lines about 1010-1210: PendingApprovalRegistry, McpDispatcher with `org_mode=False`, LiveStepUpConfig, WebServer with `controller=`), `_local_effective_org_config`, the `SettingsController` construction in `run_app` (about line 2475), the update timer (`_run_update_check_timer`, about line 319, and its start at about 2519), the LOCAL_MODE_DEPRECATION warning log (about 2421-2422), the `agent_label.set_local_mode` call (about 2424; call `agent_label.set_local_mode(False)` nowhere: step 4 removes the function's use), and in `main()` the privilege_separation calls (`check_runtime_identity`, `enforce_separation`, `SeparationHandover`, `dev_unseparated_notice`, about lines 3132-3235) and `privilege_separation.is_enabled` / `audit_layout` uses (about 850-862). Keep `configure_file_bridge()` (about line 993): organization exports use it.
      3. agent_label.py: delete `set_local_mode`, `is_local_mode`, `_local_mode` and the "Undetected" branch of `label_for` (about lines 61 and 119) so the default is the organization label. Delete the `agent_label.set_local_mode(True)` reset in tests/conftest.py (about line 155).
      4. tests/conftest.py: add an autouse fixture default `monkeypatch.setenv("PRIVACYFENCE_MODE", "org")` only if the existing environment-clearing block (about lines 188-192) removes it; keep that block.
      5. Tests: in tests/unit/test_daemon_main.py delete the local `run_app` tests (`TestRunApp*` that build a local app, the LOCAL_MODE_DEPRECATION log test at about lines 4262-4281) and set the organization mode in every remaining `run_app`/`main` test through the fixture of step 4; in tests/unit/test_org_mode.py replace `resolve_mode({}) == "local"` (about line 22) by `== "org"` and add TestRefuseLocalMode (both sources, the exact texts, `org` accepted, other value refused, disagreeing bundle and variable still refused); in tests/unit/test_agent_label.py delete TestLocalMode (about line 93); in tests/unit/web/test_agent_attestation.py delete `TestLocalOverrideNeverAttests` (it calls `daemon_main._maybe_start_web_server` with a local configuration, about line 211) and the `privilege_separation.is_enabled` patches (about lines 24 and 200).
      6. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence.
      Stop with status=blocked if a local-branch line you must delete is also reached by the organization branch (a shared helper): list it and leave it in place.
    acceptance:
      - grep -n "DEFAULT_MODE" src/privacyfence/org_mode.py shows DEFAULT_MODE = "org"
      - grep -n "set_local_mode\|_local_effective_org_config\|SettingsController\|privilege_separation" src/privacyfence/daemon_main.py src/privacyfence/agent_label.py prints nothing
      - python3 -m pytest tests/unit/test_org_mode.py tests/unit/test_daemon_main.py tests/unit/test_agent_label.py -q passes
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p05-startup-data-check
    title: Refuse a data directory 8.x cannot trust
    depends_on: [p04-mode-flip]
    complexity: S
    touches:
      - src/privacyfence/data_format.py
      - src/privacyfence/daemon_main.py
      - tests/unit/test_data_format.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read first: docs/1096/plan.md section 3.3 and section 6; docs/adr/0262-7x-is-the-migration-checkpoint-for-8x.md; src/privacyfence/data_format.py (check_not_newer, read_marker, migrate_everything, the marker writer); adr/0284-8x-refuses-a-data-directory-that-is-not-ready-for-it.md.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0284 by number.
      1. In data_format.py add `MINIMUM_FORMAT = 7`, `DESKTOP_TRACES = ("web_base_url", "mcp_token")`, `PERSON_DATA = ("users", "org", "credentials", "authority")` and `check_upgrade_source(root: Path) -> None` implementing the three ordered checks of plan section 3.3 with the two texts verbatim (the desktop text and the checkpoint text, `{root}` formatted with the path). The "mode is org" test reads `root / "org" / "org_bundle.json"` and treats an unreadable or non-object file as "does not say org". The fresh-directory case writes the marker with the module's existing atomic JSON writer and the same keys the marker has today.
      2. In daemon_main.main() replace the existing `data_format.check_not_newer(...)` call by the try/except of plan section 3.3 using `paths.data_dir()` and `_refuse_to_start`, at the same position (before every one-shot command and before load_config).
      3. Before writing the code, confirm that organization mode never writes `mcp_token` or `web_base_url`: `grep -rn "mcp_token\|web_base_url" src/privacyfence --include=*.py` and read each write site. Only web/control_channel.py and web/mcp_auth.py (PerUserTokenVerifier, load_or_create_mcp_token) may write them. If any other write site is reachable in organization mode, stop with status=blocked.
      4. Tests in tests/unit/test_data_format.py (class TestCheckUpgradeSource, tmp_path as root): fresh empty directory writes the marker and returns; a second call is a no-op; directory with `users/` and no marker raises the checkpoint text; marker format 6 with `users/` raises; marker 7 with `users/` passes; `mcp_token` at the root raises the desktop text with and without a marker; `authority/web_base_url` raises the desktop text; `mcp_token` plus `org/org_bundle.json` containing {"mode": "org"} passes; corrupt `org_bundle.json` plus a trace raises; a marker newer than 7 still raises the existing newer-release text; a test that `docs/moving-to-organization-mode.md` exists in the repository. In tests/unit/test_daemon_main.py add a test that main() returns the refusal for a data directory holding `mcp_token` (patch paths.data_dir to tmp_path) and make every existing main() test patch paths.data_dir to tmp_path.
      5. Run ruff check ., python3 -m pytest tests/unit/test_data_format.py tests/unit/test_daemon_main.py -q --cov=privacyfence.data_format --cov-branch --cov-report=term-missing (100% branch coverage of the new function), python3 -m mypy src/privacyfence.
      Stop with status=blocked if paths.data_dir() resolves to the repository root in a source checkout and the test cannot isolate it by patching.
    acceptance:
      - python3 -m pytest tests/unit/test_data_format.py -q --cov=privacyfence.data_format --cov-branch --cov-report=term-missing reports no missing branch in check_upgrade_source
      - grep -n "check_upgrade_source" src/privacyfence/daemon_main.py matches
      - grep -n "check_not_newer" src/privacyfence/daemon_main.py prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p06-local-cli
    title: Remove the one-shot desktop commands and the Windows service entry
    depends_on: [p05-startup-data-check]
    complexity: S
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/std_streams.py
      - src/privacyfence/windows_service.py
      - src/_daemon_entry.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_daemon_std_streams.py
      - tests/unit/test_privilege_separation.py
      - tests/conftest.py
    brief: |
      Read first: docs/1096/plan.md sections 2 and 3.2; src/privacyfence/daemon_main.py parse_args (about line 2952) and main (about line 3100); src/privacyfence/std_streams.py.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. In daemon_main.py remove the arguments `--windows-service`, `--print-sign-in-link`, `--print-mcp-token`, the loopback sign-in flags (`--gmail-oauth`, `--drive-oauth`, `--contacts-oauth`, `--calendar-oauth`, `--tasks-oauth`, `--apps-script-oauth`, `--slack-oauth`, `--salesforce-oauth`, `--atlassian-oauth`, `--grist-oauth`, `--miro-oauth`; parse_args lines about 2961-2971), `--peek-login` and `--telegram-setup`, together with their `run_*` functions (`run_print_mcp_token`, `run_peek_login` about line 2330, the `run_*_oauth` functions, `run_telegram_setup`) and their branches in main(). Remove `_last_startup_error` and its reset in tests/conftest.py only if nothing else reads it (grep first); otherwise leave both. Remove an argument named here only if it exists in parse_args; ignore a name that does not.
      2. Delete the `ensure_std_streams` import and calls in daemon_main.py (about lines 132 and 3101), then delete src/privacyfence/std_streams.py, src/privacyfence/windows_service.py, src/_daemon_entry.py and src/_launcher_entry.py if still present, and tests/unit/test_daemon_std_streams.py.
      3. tests/unit/test_privilege_separation.py: delete only the tests of the windows_service module (grep "windows_service" in that file); the rest of the file is deleted in a later phase.
      4. tests/unit/test_daemon_main.py: delete the tests of the removed flags and functions (the `--print-*`, `--*-oauth`, `--peek-login`, `--telegram-setup` and `--windows-service` tests).
      5. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence.
      Stop with status=blocked if a removed flag appears in docs/org-mode-setup-guide.md or docs/org-mode-docker.md (grep each flag there): report which. Mentions in other docs are handled by the docs phases.
    acceptance:
      - ls src/privacyfence/std_streams.py src/privacyfence/windows_service.py fails with no such file
      - grep -n "windows-service\|print-mcp-token\|print-sign-in-link\|peek-login\|telegram-setup" src/privacyfence/daemon_main.py prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p07-server-local
    title: Remove the local branch of the web server and the per-user MCP token
    depends_on: [p01-org-test-harness, p06-local-cli]
    complexity: M
    touches:
      - src/privacyfence/web/server.py
      - src/privacyfence/web/mcp_auth.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_mcp_auth.py
    brief: |
      Read first: docs/1096/plan.md sections 2 and 3.2; src/privacyfence/web/server.py (the local build_app and the WebServer local path; `_build_org_app` at about line 1347 stays); src/privacyfence/web/mcp_auth.py.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. web/server.py: delete the local `build_app` path, the local-only parts of WebServer (the `controller=` and `mcp_token=` parameters, ControlChannelServer wiring, LocalSessionStore and BootstrapStore use, `_BootstrapMiddleware`, `_owner_only_*`, `build_scoped_token_wiring`, `copy_local_mcp_token`, `reissue_local_recovery_code`, `local_enrollment_state`, `local_status_payload`, `_local_principal_resolver`, `web_base_url` writing and the `mcp_url` writing that only the local server did). Keep everything `_build_org_app` and the org WebServer path use, including `_write_mcp_url_file` and `_clear_mcp_url_file` (organization mode writes `mcp_url`; a later phase changes where). Remove the `mount_file_bridge` import and its call (about lines 122 and 1219) and the `from .session_auth import unauthorized_html` import (about line 159); routes_file_bridge.py keeps defining `mount_file_bridge` until phase p11. After the edit WebServer takes an `org` argument as required.
      2. web/mcp_auth.py: delete `load_or_create_mcp_token`, `rotate_mcp_token`, `_mcp_token_path`, `PerUserTokenVerifier`, `preload_verifier` and the handoff-file helpers. Keep `single_token_verifier` and `MCP_TOKEN_FILE_NAME` (web/routes_mcp.py:60 and :942 and web/routes_file_bridge.py:54 still import them; phase p11 deletes them). Keep `ScopedTokenVerifier`, `CompositeTokenVerifier`, `principal_from_access_token` and anything the org branch of server.py imports (grep the imports of web/mcp_auth across src before deleting).
      3. Tests: in tests/unit/web/test_server.py delete the classes `TestWebServerControlChannel`, `TestBootstrapFlow`, `TestNoBootstrapUrlFiles`, `TestMcpUrlFile` (local part only), `TestHumanSessionWiring`, `TestSettingsIsOwnerOnly`, `TestSettingsFoldedIntoTheCombinedApp`, `TestLocalModeWiresTheFirstEnrollmentGate`, `TestLocalEnrollmentState`, `TestLocalStatusPayload`, `TestRecoveryCodeDelivery`, `TestAiClientTokenDelivery`, `TestLocalPrincipalResolver`; any other test that builds a WebServer without `org=` is converted to an org server (copy the fixture the org tests in the same file use) or deleted if it asserts a local-only behaviour. In tests/unit/web/test_mcp_auth.py delete `TestPerUserTokenVerifier`, `TestPerPrincipalTokenStorage`, `TestPreloadVerifier`.
      4. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q, python3 -m mypy src/privacyfence, and the coverage of the two modules: python3 -m pytest tests/unit/web/test_server.py tests/unit/web/test_mcp_auth.py -q --cov=privacyfence.web.server --cov=privacyfence.web.mcp_auth --cov-branch --cov-report=term-missing; compare with the floors in scripts/check_coverage_floor.py for those two modules and add a test for any branch that dropped below its floor.
      Stop with status=blocked if a symbol you must delete is imported by a route module that stays (grep before deleting) and the import is not in a local-only function.
    acceptance:
      - grep -n "PerUserTokenVerifier\|load_or_create_mcp_token\|ControlChannelServer\|BootstrapStore" src/privacyfence/web/server.py src/privacyfence/web/mcp_auth.py prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p08a-routes-settings-admin
    title: Remove the local settings and admin route factories
    depends_on: [p07-server-local]
    complexity: M
    touches:
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/routes_admin.py
      - src/privacyfence/web/org_settings_scope.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_routes_org_config_upload.py
      - tests/unit/web/test_routes_admin.py
      - tests/unit/web/test_org_settings_scope.py
      - tests/unit/web/test_personal_settings_routes.py
    brief: |
      Read first: docs/1096/plan.md sections 2, 3.2 and 6; src/privacyfence/web/routes_settings.py (local `build_routes` about lines 931-1560, `create_app` about 1578, `build_org_routes` from about 1615); src/privacyfence/web/routes_admin.py (local `build_routes` about 754, `_local_bundle`, org `build_org_routes` about 591).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. First move tests, do not delete them: `_gate_upload`, `read_bounded_form` and `handle_org_config_upload` stay in routes_settings.py because routes_admin.py imports them (about lines 77-82, 465, 553, 869). Move the test classes that cover them (the TestOrgConfigUpload* classes of tests/unit/web/test_routes_settings.py, about lines 1087-1335 and 1931) into the new file tests/unit/web/test_routes_org_config_upload.py with their fixtures, and run that file green before the next step.
      2. routes_settings.py: delete the local `build_routes`, `create_app`, `_local_personal_hooks` (about line 1474), the local deprecation strip (about line 1056) and every helper only they use; keep `build_org_routes`, the `_ORG_ALLOWED_ACTIONS` handlers and the three shared functions above. routes_admin.py: delete the local `build_routes` and `_local_bundle`. org_settings_scope.py: replace each `LOCAL_MODE` constant use by its organization value and delete the local branches.
      3. Delete tests/unit/web/test_routes_settings.py entirely after step 1 (its organization coverage is in tests/unit/web/test_routes_org_settings.py and test_org_settings_scope.py; run both and add a test to those for any organization behaviour that only the deleted file covered, naming it for the behaviour). In tests/unit/web/test_routes_admin.py delete the `LocalWorld` fixture and the `TestLocal*` classes (about lines 1238-1515). In tests/unit/web/test_org_settings_scope.py delete the local-mode cases. tests/unit/web/test_personal_settings_routes.py builds `routes_settings.create_app`, `LocalSessionStore`, `SettingsController` and `update_checker` (about lines 16 and 46-75): keep each test whose route `build_org_routes` also serves (rebuild its fixture on the organization app the tests in tests/unit/web/test_routes_org_settings.py use) and delete the rest. Remove the `from .session_auth import unauthorized_html` imports from routes_settings.py (about line 189) and routes_admin.py (about line 91).
      4. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, and the coverage of routes_settings, routes_admin and org_settings_scope: python3 -m pytest tests/unit/web -q --cov=privacyfence.web.routes_settings --cov=privacyfence.web.routes_admin --cov=privacyfence.web.org_settings_scope --cov-branch --cov-report=term-missing; compare with scripts/check_coverage_floor.py floors and add tests for any shortfall.
      Stop with status=blocked if a function you must delete is imported by a module outside the local factories that stays (grep before deleting).
    acceptance:
      - ls tests/unit/web/test_routes_settings.py fails with no such file
      - ls tests/unit/web/test_routes_org_config_upload.py lists the file
      - grep -n "def build_routes\|def create_app\|_local_personal_hooks\|_local_bundle" src/privacyfence/web/routes_settings.py src/privacyfence/web/routes_admin.py prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p08b-routes-approvals-security
    title: Remove the local approvals and security route code
    depends_on: [p07-server-local]
    complexity: M
    touches:
      - src/privacyfence/web/routes_approvals.py
      - src/privacyfence/web/routes_security.py
      - src/privacyfence/web/state_stream.py
      - tests/unit/web/test_routes_approvals.py
      - tests/unit/web/test_routes_security.py
      - tests/unit/web/test_state_stream.py
    brief: |
      Read first: docs/1096/plan.md sections 2, 3.2 and 6; src/privacyfence/web/routes_approvals.py (local `create_app` about line 856, `build_routes` about 1010, `_bridge_shim`, `_local_step_up_response`); src/privacyfence/web/routes_security.py (the local parameters `deliver_recovery_code`, `confirm_enrollment`, `is_human_session`, `dev_unseparated_notice`, `_recent_mints` about line 1033, the `SIGN_IN_MINT_DECISION` import in a try/except about line 1049).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. routes_approvals.py: remove the `from .session_auth import unauthorized_html` import (about line 128), delete the local `create_app`, the local `build_routes` path, `_bridge_shim`, `_local_step_up_response` and the LocalSessionStore use (about lines 121-127). Keep what `_render_org_list_page` and the organization routes use.
      2. routes_security.py: delete the four local parameters and every branch they guard, `_recent_mints` and the whole "recent sign-ins" section that reads `control_channel.SIGN_IN_MINT_DECISION` (the swallowed ImportError would otherwise log a warning on every /security load).
      3. state_stream.py: delete the local snapshot path (functions that build the snapshot from the SettingsController); keep the organization stream.
      4. Tests: delete `TestHumanSessionRequiredToApprove` and `TestInjectShim` (tests/unit/web/test_routes_approvals.py, about lines 106 and 1300) and every test that builds the local app; delete `TestLocalModeEnrollment`, `TestRecoveryCodeDeliveredByTheCompanion` and `TestRecentSignInsSection` (tests/unit/web/test_routes_security.py, about lines 698, 1563, 1660) and local-only cases of the other classes; delete local cases in tests/unit/web/test_state_stream.py.
      5. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence and the coverage of the three modules against their floors in scripts/check_coverage_floor.py (python3 -m pytest tests/unit/web -q --cov=privacyfence.web.routes_approvals --cov=privacyfence.web.routes_security --cov=privacyfence.web.state_stream --cov-branch --cov-report=term-missing); add tests for any shortfall.
      Stop with status=blocked if web/session_auth.check_csrf, check_origin or human_session_required_json would lose their last caller (they must stay: organization routes use them).
    acceptance:
      - grep -n "SIGN_IN_MINT_DECISION\|_recent_mints\|_bridge_shim\|_local_step_up_response" src/privacyfence/web/routes_security.py src/privacyfence/web/routes_approvals.py prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p09-controller-channel
    title: Delete the settings controller, the control channel and the local session stores
    depends_on: [p08a-routes-settings-admin, p08b-routes-approvals-security]
    complexity: M
    touches:
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/control_channel.py
      - src/privacyfence/web/session_auth.py
      - src/privacyfence/update_checker.py
      - src/privacyfence/whats_new.py
      - src/privacyfence/web/routes_whats_new.py
      - src/privacyfence/oauth_loopback.py
      - tests/control_channel_client.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_control_channel.py
      - tests/unit/web/test_session_auth.py
      - tests/unit/test_update_checker.py
      - tests/unit/test_oauth_loopback.py
      - tests/unit/test_whats_new.py
      - tests/unit/web/test_org_settings_scope.py
      - tests/unit/web/test_personal_settings_routes.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/integration/test_settings_personal_transfer_browser.py
      - scripts/qa_web_smoke.py
      - scripts/qa_readme_screenshots.py
      - scripts/check_coverage_floor.py
      - tests/unit/test_check_coverage_floor.py
      - docs/adr/0166-*.md
      - docs/adr/0198-*.md
      - tests/conftest.py
    brief: |
      Read first: docs/1096/plan.md sections 2 and 3.2; src/privacyfence/update_checker.py; src/privacyfence/whats_new.py (imports `STABLE_RANK`, `is_newer`, `parse_version` from update_checker); src/privacyfence/web/session_auth.py.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Before deleting, run `grep -rnE "^\s*(from|import)\s.*(settings_controller|control_channel)|LocalSessionStore|BootstrapStore" src tests scripts --include=*.py`. Fix every hit in a file of `touches` (tests/unit/web/test_org_settings_scope.py about lines 20 and 64, tests/unit/web/test_personal_settings_routes.py about lines 16 and 61, tests/unit/web/test_routes_org_settings.py about line 67, tests/integration/test_settings_personal_transfer_browser.py about lines 24 and 94, scripts/qa_web_smoke.py about lines 90-101, scripts/qa_readme_screenshots.py about lines 73-84: delete the code or test that needs the deleted class; repoint what only needs a helper to settings_state). If a hit is in a file outside `touches`, stop with status=blocked and list it. Comments and docstrings that name the deleted modules may stay.
      2. Delete src/privacyfence/settings_controller.py, src/privacyfence/web/control_channel.py, tests/control_channel_client.py, tests/unit/test_settings_controller.py (the helper tests were moved in an earlier phase; run `grep -c "def test" tests/unit/test_settings_controller.py` first and confirm every remaining test exercises SettingsController or control_channel code), tests/unit/web/test_control_channel.py.
      3. web/session_auth.py: delete `LocalSessionStore`, `BootstrapStore`, `_companion_availability_sentence`, `unauthorized_html` (about line 365, with its entry in `__all__`; its importers were removed in the route phases; it reads `handoff_dir` and `privilege_separation`) and their helpers; keep `check_csrf`, `check_origin`, `human_session_required_json` and whatever the organization session code imports from it. The module keeps its strict-mypy override and its 100% coverage floor: delete the tests of the removed classes in tests/unit/web/test_session_auth.py and confirm 100% coverage of what is left (python3 -m pytest tests/unit/web/test_session_auth.py -q --cov=privacyfence.web.session_auth --cov-branch --cov-report=term-missing).
      4. update_checker.py: keep only `STABLE_RANK`, `parse_version` and `is_newer` and move them into whats_new.py; repoint web/routes_whats_new.py (`is_newer`) and whats_new.py itself; delete update_checker.py and tests/unit/test_update_checker.py, moving the tests of the three kept functions into tests/unit/test_whats_new.py.
      5. oauth_loopback.py: replace the body of `_default_open_browser(url)` with `import webbrowser` and `return webbrowser.open(url)`, update its docstring to say it opens the system browser, delete the test cases of tests/unit/test_oauth_loopback.py that exercised `request_open_url` (the module and the file are deleted in a later phase).
      6. tests/conftest.py: delete the imports and resets of `settings_controller` and `control_channel` (`_mint_nonces.clear()` about line 129) and the comment that names test_local_mode_system (about line 244).
      7. scripts/check_coverage_floor.py: delete the floors of `settings_controller` and `control_channel` (about lines 117 and 199) and their mentions in tests/unit/test_check_coverage_floor.py.
      8. docs/adr/0166 and docs/adr/0198 link to `settings_controller.py` (about lines 76 and 67): turn each link into plain text (permitted by docs/adr/README.md rule 2). Run `grep -rn "settings_controller.py\|control_channel.py\|update_checker.py" docs/adr` and do the same for every other link it prints.
      9. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py.
      Stop with status=blocked if tests/unit still import a deleted module after step 6.
    acceptance:
      - ls src/privacyfence/settings_controller.py src/privacyfence/web/control_channel.py src/privacyfence/update_checker.py fails with no such file
      - grep -rnE "^\s*(from|import)\s.*(settings_controller|control_channel)" src tests scripts --include=*.py prints nothing
      - python3 -m pytest tests/unit/web/test_session_auth.py -q --cov=privacyfence.web.session_auth --cov-branch --cov-report=term-missing reports 100%
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p10a-google-loopback
    title: Remove the Google loopback sign-in
    depends_on: [p09-controller-channel]
    complexity: M
    touches:
      - src/privacyfence/google_oauth.py
      - src/privacyfence/gmail_client.py
      - src/privacyfence/drive_client.py
      - src/privacyfence/calendar_client.py
      - src/privacyfence/contacts_client.py
      - src/privacyfence/tasks_client.py
      - src/privacyfence/apps_script_client.py
      - tests/unit/test_google_oauth.py
      - tests/unit/test_gmail_client.py
      - tests/unit/test_drive_client.py
      - tests/unit/test_calendar_client.py
      - tests/unit/test_contacts_client.py
      - tests/unit/test_tasks_client.py
      - tests/unit/test_apps_script_client.py
    brief: |
      Read first: docs/1096/plan.md section 3.2; src/privacyfence/google_oauth.py (`authorize_local` about line 172, the `oauth_loopback` import about line 47); src/privacyfence/web/routes_connect.py lines 362-430 (what the organization flow uses; it stays).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Run `grep -rn "authorize_local\|authorize_interactive\|oauth_loopback" src/privacyfence --include=*.py` and list every Google module hit. In google_oauth.py delete `authorize_local`, the `oauth_loopback` import and anything only they use; in each Google `*_client.py` of `touches` delete the `authorize_interactive` method (gmail_client.py about line 456, drive_client.py 1257, calendar_client.py 310, contacts_client.py 150, tasks_client.py 95, apps_script_client.py 123) and any wrapper that calls `google_oauth.authorize_local`, with their tests.
      2. Keep `web_client_config`, `authorize_url`, `exchange_code`, `save_credentials` of google_oauth.py (routes_connect.py uses them) and every function the organization routes import.
      3. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, and python3 -m pytest tests/unit/test_google_oauth.py -q --cov=privacyfence.google_oauth --cov-branch --cov-report=term-missing (compare with its floor in scripts/check_coverage_floor.py; add tests for any shortfall).
      Stop with status=blocked if routes_connect.py imports a function you must delete.
    acceptance:
      - grep -rn "authorize_local\|oauth_loopback" src/privacyfence/google_oauth.py src/privacyfence/gmail_client.py src/privacyfence/drive_client.py src/privacyfence/calendar_client.py src/privacyfence/contacts_client.py src/privacyfence/tasks_client.py src/privacyfence/apps_script_client.py prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p10b-other-loopback
    title: Remove the other connectors' loopback sign-in and the loopback module
    depends_on: [p09-controller-channel]
    complexity: M
    touches:
      - src/privacyfence/slack_client.py
      - src/privacyfence/salesforce_client.py
      - src/privacyfence/atlassian_oauth.py
      - src/privacyfence/miro_auth.py
      - src/privacyfence/grist_auth.py
      - src/privacyfence/oauth_loopback.py
      - tests/unit/test_slack_client.py
      - tests/unit/test_salesforce_client.py
      - tests/unit/test_atlassian_oauth.py
      - tests/unit/test_miro_auth.py
      - tests/unit/test_grist_auth.py
      - tests/unit/test_oauth_loopback.py
      - docs/adr/0230-*.md
    brief: |
      Read first: docs/1096/plan.md section 3.2; the `authorize_interactive` functions at slack_client.py (about 257), salesforce_client.py (about 647), atlassian_oauth.py (about 206), miro_auth.py (about 290), grist_auth.py (about 505).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Delete `authorize_interactive` (and a loopback-only helper next to it) from the five modules and their tests, and the `oauth_loopback` imports. Keep every function web/routes_connect.py and grist_provisioning.py import (the authorize-URL builders, `save_token_record`, the token exchange).
      2. Telegram's `authorize_interactive` and Peek's device login are session logins, not loopback flows: do not touch telegram_client.py or peek_client.py.
      3. After the five modules no longer import it, delete src/privacyfence/oauth_loopback.py and tests/unit/test_oauth_loopback.py; `grep -rn oauth_loopback src tests scripts` must print nothing.
      4. docs/adr/0230 links to oauth_loopback.py (about line 66): turn the link into plain text (docs/adr/README.md rule 2).
      5. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence and the coverage of each stripped module against its floor in scripts/check_coverage_floor.py; add tests for any shortfall.
      Stop with status=blocked if routes_connect.py imports a function you must delete.
    acceptance:
      - ls src/privacyfence/oauth_loopback.py fails with no such file
      - grep -rn "oauth_loopback\|authorize_interactive" src/privacyfence/slack_client.py src/privacyfence/salesforce_client.py src/privacyfence/atlassian_oauth.py src/privacyfence/miro_auth.py src/privacyfence/grist_auth.py prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p11-file-bridge
    title: Remove the file bridge, the shim handshake and the companion wording
    depends_on: [p09-controller-channel]
    complexity: M
    touches:
      - src/privacyfence/local_files.py
      - src/privacyfence/web/routes_file_bridge.py
      - src/privacyfence/web/routes_mcp.py
      - src/privacyfence/web/mcp_tools.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/web/mcp_auth.py
      - docs/tools-reference.md
      - tests/unit/test_local_files.py
      - tests/unit/web/test_routes_file_bridge.py
      - tests/unit/web/test_routes_mcp.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_mcp_auth.py
      - tests/unit/connectors/test_drive_connector.py
      - tests/unit/connectors/test_gmail_connector.py
      - tests/unit/connectors/test_confluence_connector.py
      - tests/integration/test_shim_mcp_contract.py
      - tests/conftest.py
    brief: |
      Read first: docs/1096/plan.md sections 3.5 and 6; adr/0286 (the ADR file in docs/adr whose name starts 0286-); docs/adr/0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md; src/privacyfence/local_files.py (`require_local_files` at about line 287); src/privacyfence/web/routes_file_bridge.py; src/privacyfence/web/mcp_dispatch.py (`McpDispatcher.call` at about line 255, the staged-download guard at about lines 340-350, the status fields at about lines 704 and 729-739).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. First write the failing test, in tests/unit/web/test_mcp_dispatch.py: a tool call carrying an argument that the tool's ToolSpec does not declare reaches the connector without that argument. Then implement the filter exactly as plan section 3.5 "Undeclared arguments" says, at the start of `McpDispatcher.call`. Keep the test: it is the safety net for removed parameters.
      2. local_files.py: delete `LocalFilesNeeded`, `META_KEY`, `build_need_uploads_files`, `force_bridge_for_tests`, `_deliver_direct`, `_deliver_bridge` and the bridge-collecting part of `require_local_files` (a path that is not an `upload:` reference and cannot be read directly raises `LocalFileAccessError(NO_BRIDGE_UPLOAD_MESSAGE)`); rewrite `NO_BRIDGE_UPLOAD_MESSAGE` as plan section 3.5 gives; make `can_access_user_files` return `False` unconditionally (keep its `download_mode` parameter); make `deliver_file` keep only its link branch; remove the `privilege_separation` import and replace each `is_enabled()` use by `False`, deleting the dead branch. Keep every name plan section 3.5 lists as kept. Connectors still call `can_access_user_files`, `deliver_file`, `require_local_files` and use `download_mode`; the connector phases remove those.
      3. routes_file_bridge.py: keep `mount_capability_routes`, `/mcp-files/slots/{slot}`, `/mcp-files/fetch/{token}` and their helpers; delete the bearer router, `build_file_bridge_asgi_app`, `mount_file_bridge`, the `/uploads` and `/downloads` handlers and the import of `single_token_verifier`. web/mcp_auth.py: delete `single_token_verifier` and `MCP_TOKEN_FILE_NAME`; in routes_mcp.py remove the import (about line 60) and the branch of `build_mcp_asgi_app` that uses it (about line 942), keeping the verifier the organization branch builds; if the organization branch also needs `single_token_verifier`, stop with status=blocked.
      4. routes_mcp.py: delete the `X-PrivacyFence-File-Bridge` header handling, `_need_uploads_result`, the `except local_files.LocalFilesNeeded` clause and the `deliver` `_meta` block (about lines 291-310 and 505-567); rewrite `SERVER_INSTRUCTIONS` (about line 96) per plan section 3.5. mcp_tools.py: rewrite the `privacyfence_status` description (about lines 264-267) to drop the companion and deprecation wording. mcp_dispatch.py: delete the `deprecation` field and `next_step: open_privacyfence_companion` (about lines 704 and 729-739) and the `_mode == "local"` branches (about lines 542, 559, 704, 717); make the organization staging paths set `staged_download` on the call state and add a test that a repeated identical call does not mint a second one-time link.
      5. Tests: delete tests/integration/test_shim_mcp_contract.py; delete `TestFileBridgeOverTheWire` (tests/unit/web/test_routes_mcp.py about line 486), the local status and deprecation test (tests/unit/web/test_mcp_dispatch.py about line 974), the bridge tests of tests/unit/test_local_files.py and tests/unit/web/test_routes_file_bridge.py (keep the tests of the upload, capability and deliver_export code), the tests of `single_token_verifier` in tests/unit/web/test_mcp_auth.py, and in tests/unit/connectors/test_drive_connector.py, test_gmail_connector.py and test_confluence_connector.py every test that needs `force_bridge_for_tests` or the bridge handshake (a test that only used it to pick the direct-disk path is deleted too). In tests/conftest.py delete the `local_files.force_bridge_for_tests(False)` reset (about line 125) but keep the `configure_file_bridge()` call.
      6. Regenerate: python3 scripts/generate_tools_reference.py then python3 -m pytest tests/unit/test_docs_tools_reference.py -q.
      7. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q, python3 -m mypy src/privacyfence, and the coverage of local_files, routes_file_bridge, routes_mcp, mcp_dispatch, mcp_auth against their floors in scripts/check_coverage_floor.py (python3 -m pytest tests/unit -q --cov=privacyfence --cov-branch --cov-report=term-missing and read those modules); add tests for any shortfall.
      Stop with status=blocked if grep finds `LocalFilesNeeded` outside local_files.py and routes_mcp.py.
    acceptance:
      - grep -rn "LocalFilesNeeded\|META_KEY\|X-PrivacyFence-File-Bridge\|open_privacyfence_companion\|single_token_verifier" src prints nothing
      - python3 -m pytest tests/unit/web/test_mcp_dispatch.py -q passes
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p12a-google-connectors
    title: Remove download_mode and path parameters from Drive and Gmail
    depends_on: [p11-file-bridge, p10a-google-loopback, p10b-other-loopback]
    complexity: M
    touches:
      - src/privacyfence/connectors/drive.py
      - src/privacyfence/connectors/gmail.py
      - src/privacyfence/gmail_client.py
      - src/privacyfence/drive_client.py
      - src/privacyfence/local_files.py
      - docs/tools-reference.md
      - tests/unit/connectors/test_drive_connector.py
      - tests/unit/connectors/test_gmail_connector.py
      - tests/unit/test_gmail_client.py
      - tests/unit/test_drive_client.py
    brief: |
      Read first: docs/1096/plan.md section 3.5; adr/0286-files-move-by-upload-slot-inline-bytes-or-one-time-link-only.md; src/privacyfence/connectors/drive.py (`download_mode` default about line 161, `_upload_file` about 1677-1740, the download methods and `_deliver_org_download`); src/privacyfence/connectors/gmail.py (`download_mode` about line 325 and uses about 1116-1256, 1482-1641); src/privacyfence/gmail_client.py about lines 119-140 and 906.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      0. In local_files.py give every function that has a `download_mode` parameter the default `download_mode: str = "org"` (a later phase deletes the parameter), so callers can drop the argument one connector at a time.
      1. Remove the `download_mode` attribute and every parameter of that name, and every `download_mode != "org"` branch, from drive.py, gmail.py, gmail_client.py and drive_client.py; keep the organization branch as the only path (inline base64 when it fits `DownloadDeliveryConfig.inline_max_bytes`, else the staged one-time link). Remove `destination_dir` and plain `local_path` from the ToolSpec params and the method signatures of the tools in these two connectors. Drive upload and Gmail attachments accept `upload:<id>` references only; a path that does not start with `local_files.UPLOAD_REF_PREFIX` raises the connector's existing error type with the text from plan section 3.5 (`NO_BRIDGE_UPLOAD_MESSAGE`).
      2. Delete the plain-path branches: the `is_org_local_path` branch of drive `_upload_file` and the non-`upload:` branch of `gmail_client._read_attachment`; delete every call to `local_files.can_access_user_files` and `local_files.deliver_file` in these two connectors.
      3. Update the tool descriptions per ADR 0115 and regenerate docs/tools-reference.md with python3 scripts/generate_tools_reference.py.
      4. Tests: rewrite the affected tests in tests/unit/connectors/test_drive_connector.py, test_gmail_connector.py, tests/unit/test_gmail_client.py and tests/unit/test_drive_client.py to the single organization path; add one test per changed tool that a non-`upload:` path is refused with that text.
      5. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m pytest tests/unit/test_docs_tools_reference.py -q, python3 -m mypy src/privacyfence and the coverage of the four modules against their floors in scripts/check_coverage_floor.py.
      Stop with status=blocked if a ToolSpec change makes tests/unit/test_connector_catalog*.py or the tool-annotation tests (ADR 0089) fail for a reason other than the removed parameters.
    acceptance:
      - grep -rn "download_mode\|destination_dir" src/privacyfence/connectors/drive.py src/privacyfence/connectors/gmail.py src/privacyfence/gmail_client.py src/privacyfence/drive_client.py prints nothing
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p12b-other-connectors
    title: Remove download_mode and path parameters from Salesforce, Jira and Assets
    depends_on: [p12a-google-connectors]
    complexity: M
    touches:
      - src/privacyfence/connectors/salesforce.py
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/connectors/assets.py
      - docs/tools-reference.md
      - tests/unit/connectors/test_salesforce_connector.py
      - tests/unit/connectors/test_jira_connector.py
      - tests/unit/connectors/test_assets_connector.py
    brief: |
      Read first: docs/1096/plan.md section 3.5; the ADR file in docs/adr whose name starts 0286-; src/privacyfence/connectors/salesforce.py, jira.py, assets.py (grep `download_mode`, `destination_dir`, `can_access_user_files`, `deliver_file`, `local_path`).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. In each of the three connectors delete the `download_mode` attribute, every parameter of that name, every `!= "org"` branch, every call to `local_files.can_access_user_files` and `local_files.deliver_file`, and `destination_dir` and plain `local_path` from ToolSpec params and method signatures; the organization path (inline base64, or the staged one-time link) is the only code left.
      2. Update the tool descriptions per ADR 0115 and regenerate docs/tools-reference.md with python3 scripts/generate_tools_reference.py.
      3. Tests: rewrite the affected tests of the three connector test files to the single organization path; add one test per changed tool that a non-`upload:` path, where the tool takes one, is refused with the text of `local_files.NO_BRIDGE_UPLOAD_MESSAGE`.
      4. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m pytest tests/unit/test_docs_tools_reference.py -q, python3 -m mypy src/privacyfence, and the coverage of the three connectors against their floors in scripts/check_coverage_floor.py.
      Stop with status=blocked if a ToolSpec change makes the tool-annotation or catalog tests fail for a reason other than the removed parameters.
    acceptance:
      - grep -rn "download_mode\|destination_dir" src/privacyfence/connectors/salesforce.py src/privacyfence/connectors/jira.py src/privacyfence/connectors/assets.py prints nothing
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p12c-remaining-connectors
    title: Remove download_mode and path parameters from Grist, Peek and Confluence and delete the parameter
    depends_on: [p12b-other-connectors]
    complexity: M
    touches:
      - src/privacyfence/connectors/grist.py
      - src/privacyfence/connectors/peek.py
      - src/privacyfence/connectors/confluence.py
      - src/privacyfence/confluence_client.py
      - src/privacyfence/export_files.py
      - src/privacyfence/local_files.py
      - src/privacyfence/daemon_main.py
      - docs/tools-reference.md
      - tests/unit/connectors/test_grist_connector.py
      - tests/unit/connectors/test_peek_connector.py
      - tests/unit/connectors/test_confluence_connector.py
      - tests/unit/test_export_files.py
      - tests/unit/test_local_files.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read first: docs/1096/plan.md section 3.5; the ADR file in docs/adr whose name starts 0286-; src/privacyfence/export_files.py (`DESTINATION_ERROR`, `check_destination`); src/privacyfence/daemon_main.py (about lines 1695-2032, the `download_mode` computation and the `connector.download_mode = ...` assignments in build_connectors).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Grist, Peek, Confluence (and confluence_client.py): delete the `download_mode` attribute, every parameter of that name, every `!= "org"` branch, every call to `local_files.can_access_user_files` and `local_files.deliver_file`, and `destination_dir` and plain `local_path` from ToolSpec params and signatures. Peek and Grist already refuse non-`upload:` paths in organization mode: make that the only behaviour and keep their error texts.
      2. export_files.py: delete `DESTINATION_ERROR`, `check_destination` and the local `destination_dir` handling; keep `visible_specs`, `refuse_if_exports_off`, `refuse_org_without_staging`.
      3. local_files.py: delete `can_access_user_files` and `deliver_file` and every `download_mode` parameter of `read_local_file`, `local_file_size`, `deliver_export`, `check_delivery_size` and `require_local_files`, with their callers.
      4. daemon_main.py: delete the `download_mode = org_mode.resolve_mode(...)` computation (about line 1695) and every `connector.download_mode = ...` assignment (about lines 1726-2032).
      5. Update the tool descriptions per ADR 0115 and regenerate docs/tools-reference.md with python3 scripts/generate_tools_reference.py.
      6. Tests: rewrite the affected tests of the files in `touches` to the single organization path; add one test per changed tool that a non-`upload:` path, where the tool takes one, is refused with the text of `local_files.NO_BRIDGE_UPLOAD_MESSAGE`.
      7. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m pytest tests/unit/test_docs_tools_reference.py -q, python3 -m mypy src/privacyfence, and the coverage of every changed module against its floor in scripts/check_coverage_floor.py.
      Stop with status=blocked if grep finds `download_mode` in a module outside `touches` after step 4.
    acceptance:
      - grep -rn "download_mode\|destination_dir\|can_access_user_files" src prints nothing
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p12d-live-qa
    title: Record the live QA report for the touched connectors
    depends_on: [p12c-remaining-connectors]
    complexity: S
    touches:
      - tests/fixtures/live/**
    brief: |
      Read first: docs/connector-qa.md (the sections on recording and "Reviewing recorded fixtures"); .tld/toolkit.yaml `ci.dispatchable` entry for qa-record-fixture.yml; .github/workflows/qa-record-fixture.yml (header comment and inputs).
      1. Push this phase's branch (the worker's own branch) so the workflow can check it out.
      2. For each connector in this order: gmail, drive, confluence, jira, assets, salesforce, grist, peek, dispatch `qa-record-fixture.yml` on `main` (the self-hosted runner admits only workflows as they stand on `main`) with inputs `connector=<name>` and `branch=<this phase's branch>`. The workflow shares a concurrency group (`connector-live-check`): a queued run is correct, wait for it; never dispatch the same connector twice. Read each run's result and its job log.
      3. After the last run, `git pull` this branch (the workflow commits recorded fixtures to it) and read every diff under tests/fixtures/live with the rule of docs/connector-qa.md "Reviewing recorded fixtures": no account identifier, tenant URL, token or private content may enter the repository. If a diff contains one, stop with status=blocked and name the file; do not commit it.
      4. Report one line per connector: run URL, conclusion, whether fixtures changed.
      Stop with status=blocked if a dispatch is refused, a run fails twice on the same commit (do not rerun a second failure away), or the runner is offline.
    acceptance:
      - the run of qa-record-fixture.yml for each of the eight connectors concluded success (reported with its URL)
      - git diff origin/feature/drop-desktop-mode --stat -- tests/fixtures/live lists only fixture files, each reviewed

  - id: p13-privilege-separation
    title: Delete privilege separation and the Windows ACL code
    depends_on: [p12d-live-qa]
    complexity: M
    touches:
      - src/privacyfence/privilege_separation.py
      - src/privacyfence/windows_acl.py
      - src/privacyfence/paths.py
      - src/privacyfence/secure_files.py
      - src/privacyfence/step_up_config.py
      - src/privacyfence/audit_log.py
      - src/privacyfence/local_files.py
      - src/privacyfence/web/server.py
      - scripts/check_coverage_floor.py
      - tests/unit/test_check_coverage_floor.py
      - tests/unit/test_code_no_history.py
      - tests/unit/web/test_agent_attestation.py
      - scripts/linux_privilege_separation.sh
      - scripts/macos_privilege_separation.sh
      - scripts/windows_privilege_separation.ps1
      - tests/unit/test_privilege_separation.py
      - tests/unit/test_windows_acl.py
      - tests/unit/test_paths.py
      - tests/unit/test_secure_files.py
      - tests/unit/test_step_up_config.py
      - tests/unit/test_audit_log.py
      - tests/integration/test_org_guide_configs.py
      - tests/conftest.py
    brief: |
      Read first: docs/1096/plan.md sections 3.2 and 6; src/privacyfence/paths.py (`data_dir`, the `handoff_dir`, `control_socket_dir`, `authority_dir` and the privilege_separation calls about lines 150-203, `windows_data_dir`, `is_bundled`); src/privacyfence/step_up_config.py about lines 211 and 412; src/privacyfence/audit_log.py about lines 1131-1139; src/privacyfence/secure_files.py about lines 99-103.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Pin the five survivors to their organization values first: paths.py — in `data_dir()` delete exactly the two lines that call `privilege_separation.data_dir_override()` and return its `secure_mkdir`, and the `privilege_separation` import; keep the rest of `data_dir()` as it is, and keep `is_bundled`, `windows_data_dir`, `is_windows` and `authority_dir` (`data_dir()` and the audit log use them); delete `handoff_dir`, `control_socket_dir`, `bundle_macos_dir` and `app_bundle_path` when they exist and nothing else imports them (a diff to `data_dir()` beyond the two lines is a stop condition); web/server.py — `_write_mcp_url_file` writes `paths.data_dir() / MCP_URL_FILE_NAME` with `secure_files.atomic_write_text(path, url)` and `_clear_mcp_url_file` unlinks that same path (on an unseparated install `handoff_dir()` was already `data_dir()`, so nothing moves); drop the docstring text about the handoff directory; secure_files.py — delete the `shared_dir_mode` hook and the `write_handoff_file` comment; step_up_config.py — replace `is_enabled() or dev_allows_unseparated()` by `False` at both sites and delete the dead branches; audit_log.py — if its use of `windows_data_dir`/`is_windows` (about lines 1131-1139) depends on privilege separation, replace only that dependency by the unseparated value; the resolved audit directory on Linux must not change; local_files.py — if any privilege_separation reference is left, replace it by its organization value.
      2. Confirm with `grep -rnE "^\s*(from|import)\s.*(privilege_separation|windows_acl)" src scripts tests --include=*.py` that only the files in `touches` still import them, then delete src/privacyfence/privilege_separation.py, src/privacyfence/windows_acl.py, the three scripts/*_privilege_separation.* files, tests/unit/test_privilege_separation.py and tests/unit/test_windows_acl.py.
      3. tests/integration/test_org_guide_configs.py: delete the "shipped Linux daemon unit" section (about lines 199-230) that reads scripts/linux_privilege_separation.sh; this file is part of the required org-mode-smoke check, so run it: python3 -m pytest tests/integration/test_org_guide_configs.py -q.
      4. tests/conftest.py: delete the `privilege_separation` import and `privilege_separation.reset_cache()` (about line 135). scripts/check_coverage_floor.py and its test: delete the floors of `privilege_separation` and `windows_acl` (about lines 138 and 153). tests/unit/test_code_no_history.py (about lines 71-72) and tests/unit/web/test_agent_attestation.py: remove the references to the deleted modules.
      5. Strip the tests of paths, secure_files, step_up_config and audit_log to the organization behaviour (delete the cases for the separated root, the shared-dir mode and the unseparated notice).
      6. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py, and the coverage of paths, secure_files, step_up_config and audit_log against their floors in scripts/check_coverage_floor.py (secure_files keeps 100%).
      Stop with status=blocked if `paths.data_dir()` returns a different directory on Linux after your change (compare with the value before the change in a throwaway test).
    acceptance:
      - ls src/privacyfence/privilege_separation.py src/privacyfence/windows_acl.py fails with no such file
      - grep -rnE "^\s*(from|import)\s.*(privilege_separation|windows_acl)" src tests scripts --include=*.py prints nothing
      - python3 -m pytest tests/integration/test_org_guide_configs.py -q passes
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p14a-ui-renderers
    title: Strip the local branches from the settings and approval page renderers
    depends_on: [p09-controller-channel]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/approval_window_html.py
      - src/privacyfence/approval_list_html.py
      - src/privacyfence/dialog_window_html.py
      - src/privacyfence/web_prompt.py
      - src/privacyfence/web_approval_ui.py
      - src/privacyfence/web_shell.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/test_approval_window_html.py
      - tests/unit/test_approval_list_html.py
      - tests/unit/test_dialog_window_html.py
      - tests/unit/test_web_prompt.py
      - tests/unit/test_web_shell.py
      - tests/integration/test_settings_*_browser.py
      - scripts/render_ui_review.py
      - scripts/qa_readme_screenshots.py
    brief: |
      Read first: docs/1096/plan.md sections 2 and 3.2; src/privacyfence/settings_window_html.py (`_capabilities_for` about line 3438, `_admin_capabilities` about 3408, the `LOCAL_MODE` constants, the `CAPS.mode !== 'org'` JavaScript about lines 661, 1209, 2322, the `ai_client` section); src/privacyfence/web_shell.py (`_NAV_ITEMS` about line 513, `local_mode_deprecation_html` about line 635).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. settings_window_html.py: `build_html` keeps working for the organization Settings and Admin pages (routes_admin.py and the org settings routes call it with `mode="org"`). Remove the `mode` parameter's local value: delete every local-only branch, the `LOCAL_MODE` set, the `CAPS.mode !== 'org'` JavaScript, the local bundle-upload UI and the `ai_client` section; the parameter itself is removed in a later phase, so keep `mode` accepted and ignored with default "org".
      2. approval_window_html.py: keep the card builder and nonce extraction used by gate.py, card_builder.py, dialog_window_html.py and routes_approvals.py; delete the native-window and shim bits. approval_list_html.py: strip the local agent-label bits only. dialog_window_html.py: delete `build_choice_html` and `PICKER_WIDTH` (only the deleted settings controller used them). web_prompt.py: delete `block_on_choice`. web_approval_ui.py: keep the organization path; delete only a local branch if grep finds one. web_shell.py: delete `local_mode_deprecation_html`, the local `_NAV_ITEMS` and the desktop strip; keep `ORG_NAV_ITEMS`.
      3. Tests: strip the matching cases of the seven unit test files; the six `tests/integration/test_settings_*_browser.py` files call `build_html` with the default mode and import `tests.unit.test_settings_window_html._make_state`: keep them, make them use mode "org", and keep `_make_state` working. Update scripts/render_ui_review.py and scripts/qa_readme_screenshots.py so they render the organization pages; if either imports Pillow, keep the import until the dependency phase decides (grep first).
      4. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, python3 -m pytest tests/integration/test_settings_*_browser.py -q (browser tests may report SKIPPED when Chromium cannot launch; say so), and the coverage of the changed modules against their floors in scripts/check_coverage_floor.py.
      Stop with status=blocked if an organization page loses an element in the rendered HTML (compare `build_html(mode="org", ...)` output for the Admin and Settings states before and after in a throwaway script and report any difference that is not a removed local element).
    acceptance:
      - grep -n "LOCAL_MODE\|local_mode_deprecation_html\|build_choice_html\|block_on_choice" src/privacyfence/settings_window_html.py src/privacyfence/web_shell.py src/privacyfence/dialog_window_html.py src/privacyfence/web_prompt.py prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p14b-mode-validation
    title: Replace resolve_mode by a validation and delete the deprecation text
    depends_on: [p13-privilege-separation, p14a-ui-renderers]
    complexity: M
    touches:
      - src/privacyfence/org_mode.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/privacy_filter.py
      - src/privacyfence/agent_label.py
      - tests/unit/test_org_mode.py
      - tests/unit/test_daemon_main.py
      - tests/conftest.py
    brief: |
      Read first: docs/1096/plan.md sections 3.2 and 3.4; src/privacyfence/org_mode.py (`resolve_mode` about line 94, `Mode`, `LOCAL_MODE_DEPRECATION` about line 67, `is_single_user` about line 146, `_idp_present`, the guards at about lines 847 and 900); the `resolve_mode` call sites: `grep -rn "resolve_mode" src --include=*.py`.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. org_mode.py: replace `resolve_mode` by `validate_mode(org_config: dict[str, Any]) -> None` with the same checks and the same texts as plan section 3.4 and no return value. Delete `Mode`, `DEFAULT_MODE`, `LOCAL_MODE_DEPRECATION` and their `__all__` entries (keep `refuse_local_mode`). `is_single_user(org_config)` returns `not _idp_present(org_config)`. The two guards `if resolve_mode(org_config) != "org": return ...` (about lines 847 and 900) are deleted with their early return.
      2. daemon_main.py: call `org_mode.validate_mode(...)` exactly twice: in the function that contains the call at about line 553 (where the bundle is loaded) and at the start of `run_app` (about line 2406). Every other `resolve_mode` call (about lines 630, 679, 699, 975, 2414, 2415, 2436, 2761, and the docstring at about line 491) drops the call, the `mode=` keyword it fed and the branch that tested it (`== "org"` is always true; `== "local"` branches were deleted earlier). `mode` parameters that other modules still declare are removed in the next phase: pass nothing new; if a callee still requires `mode`, pass the literal `"org"` and leave the parameter for the next phase.
      3. privacy_filter.py (docstring about line 99) and agent_label.py (docstring about line 25): reword so neither names `resolve_mode` nor says the default is local.
      4. tests/unit/test_org_mode.py: rename the resolve_mode tests to `validate_mode` and keep every text assertion; delete the DEFAULT_MODE and deprecation tests. tests/unit/test_daemon_main.py and tests/conftest.py: remove references to the deleted names.
      5. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py, and the coverage of org_mode and daemon_main against their floors.
      Stop with status=blocked if a surviving module outside `touches` calls `resolve_mode`, `DEFAULT_MODE` or `LOCAL_MODE_DEPRECATION` (list it: the next phase may need it in `touches`).
    acceptance:
      - grep -rn "resolve_mode\|LOCAL_MODE_DEPRECATION\|DEFAULT_MODE" src prints nothing except lines in files of the next phase's touches
      - python3 -m pytest tests/unit/test_org_mode.py -q passes
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p14c-mode-parameters
    title: Remove the mode parameters from the configuration functions
    depends_on: [p14b-mode-validation]
    complexity: M
    touches:
      - src/privacyfence/admin_config.py
      - src/privacyfence/oauth_apps.py
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/grist_auth.py
      - src/privacyfence/peek_client.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/web/routes_admin.py
      - src/privacyfence/web/routes_connect.py
      - src/privacyfence/web/server.py
      - tests/unit/test_admin_config.py
      - tests/unit/test_oauth_apps.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_grist_auth.py
      - tests/unit/test_peek_client.py
    brief: |
      Read first: docs/1096/plan.md section 3.2; `grep -rn "mode=\|org_mode=\|mode: Mode" src --include=*.py` (the full call list); src/privacyfence/admin_config.py (`config_for` about line 440, callers about lines 945 and 1035); src/privacyfence/oauth_apps.py (`effective_org_config` about line 327).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Remove the parameter that only distinguished local from organization mode, keeping the organization behaviour: `admin_config.config_for(bundle, *, mode, single_user)` becomes `config_for(bundle, *, single_user)` and the same for the two functions that call it (about lines 945 and 1035) and their `mode` parameter; `oauth_apps.effective_org_config(..., mode=...)` loses `mode`; `scoped_tokens.policy_from_org_config(org_config, *, org_mode)` loses `org_mode` (about line 192; the local value `False` was only used by the deleted local server); `grist_auth.bundle_settings(..., org_mode=...)` and `peek_client.bundle_base_url(..., org_mode=...)` lose `org_mode` and behave as `org_mode=True`.
      2. Update every caller: daemon_main.py (about lines 676-705, 1507, 2011, 2026, 2231, 2332), web/routes_admin.py (about lines 315 and 621), web/routes_connect.py (about lines 130 and 155), web/server.py (about line 1470).
      3. Update the tests of the changed functions to the new signatures; delete cases that exercise the local value.
      4. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py and the coverage of the changed modules against their floors.
      Stop with status=blocked if `grep -rn "mode=\"local\"\|mode='local'" src` is not empty after step 2.
    acceptance:
      - grep -rn "org_mode=\|mode=\"local\"" src prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p14d-mode-leftovers
    title: Remove the remaining local-mode switches from the UI and step-up code
    depends_on: [p14c-mode-parameters]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/routes_admin.py
      - src/privacyfence/web/routes_approvals.py
      - src/privacyfence/web/org_settings_scope.py
      - src/privacyfence/step_up_config.py
      - src/privacyfence/webauthn_stepup.py
      - src/privacyfence/connector.py
      - src/privacyfence/catalog_server.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/test_step_up_config.py
      - tests/unit/test_webauthn_stepup.py
      - tests/unit/test_catalog_server.py
      - tests/conftest.py
    brief: |
      Read first: docs/1096/plan.md sections 3.2 and 3.7; `grep -rn "mode" src/privacyfence/settings_window_html.py | head -60`; `grep -rn "from_local_config\|default_local_step_up\|local_enrollment_banner\|_mode\b" src --include=*.py`.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. settings_window_html.py: delete the `mode` parameter of `build_html` (kept and ignored since the renderer phase) and of the helpers that took it, and update the callers in web/routes_settings.py, web/routes_admin.py, web/routes_approvals.py and web/org_settings_scope.py (delete the `LOCAL_MODE` constants that remain).
      2. step_up_config.py: delete `from_local_config`, `default_local_step_up` and `local_enrollment_banner`; webauthn_stepup.py: delete the local-enrollment pieces; connector.py: delete the `_mode`/`download_mode` fields that remain (grep first); keep every branch the organization path uses.
      3. catalog_server.py (about lines 26 and 33): point `https://privacyfence.eu/download/` at `https://privacyfence.eu/install/` and fix tests/unit/test_catalog_server.py.
      4. tests/conftest.py: delete resets whose subject no longer exists (grep).
      5. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py and the coverage of the changed modules against their floors.
      Stop with status=blocked if a removed function is imported by a module outside `touches`.
    acceptance:
      - grep -rn "from_local_config\|default_local_step_up\|local_enrollment_banner" src prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p15a-packaging
    title: Delete the installers, packaging files, the extension and the desktop-only tools
    depends_on: [p13-privilege-separation]
    complexity: M
    touches:
      - PrivacyFenceApp.spec
      - PrivacyFenceApp.linux.spec
      - PrivacyFenceApp.win.spec
      - com.privacyfence.app.plist
      - privacyfence.service
      - installer/**
      - debian/**
      - resources/linux/**
      - mcpb/**
      - scripts/build_dmg.sh
      - scripts/build_pkg.sh
      - scripts/build_installer.ps1
      - scripts/build_deb.sh
      - scripts/build_mcpb.sh
      - scripts/pyinstaller_common.py
      - scripts/check_deb_glibc_floor.py
      - scripts/macos_sign_retry.sh
      - scripts/entitlements.plist
      - scripts/dev_start.sh
      - tests/unit/test_dev_start.py
      - tests/unit/test_pyinstaller_hidden_imports.py
      - tests/unit/test_macos_sign_retry.py
      - tests/unit/test_macos_uninstall_message.py
      - tests/unit/test_builtin_apps_script.py
      - tests/unit/test_telegram_credentials.py
      - tests/diagnostics.py
      - .tld/toolkit.yaml
      - .claude/hooks/session-start.sh
      - .claude/commands/cut-release.md
      - .claude/skills/steward/SKILL.md
      - .github/pull_request_template.md
      - .github/workflows/tests.yml
      - .gitignore
    brief: |
      Read first: docs/1096/plan.md section 3.6; .tld/toolkit.yaml; .claude/hooks/session-start.sh (the `cd mcpb/shim && npm ci` lines about 78-79); .github/pull_request_template.md.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Delete every path in `touches` that is a desktop packaging file, build script, `mcpb/` or `installer/`/`debian/`/`resources/linux/`. Before deleting each script, run `grep -rln "<script name>" . --exclude-dir=.git --exclude-dir=node_modules` and fix every hit that is in a file of `touches`; list any other hit in your final report (docs, build.yml and the other workflows belong to later phases).
      2. Delete the tests of deleted things: tests/unit/test_dev_start.py, test_pyinstaller_hidden_imports.py, test_macos_sign_retry.py, test_macos_uninstall_message.py. Keep tests/unit/test_setup_py.py (it tests the changelog build hook of ADR 0237). In tests/unit/test_builtin_apps_script.py (about line 282) and tests/unit/test_telegram_credentials.py (about line 163) remove the assertions that read `build_dmg.sh`, `build_deb.sh` and `build_installer.ps1`, keeping every assertion about the scripts that stay. Edit tests/diagnostics.py to drop desktop packaging diagnostics (grep it for dmg, pkg, deb, installer, companion).
      3. .tld/toolkit.yaml: delete the two `verify.dod` entries that run `npm test` and `npm run typecheck` in `mcpb/shim`; delete the `dod_conditional` entries whose `when` is `mcpb/shim/src/**` (keep the other file patterns of that entry only if it lists more than the shim; the entry for `web/mcp_auth.py` and `web/server.py` stays with the shim glob removed, and its `do:` text says `pytest tests/integration -v`, and test_mcp_daemon_contract.py must pass` instead of naming test_shim_mcp_contract.py) and the entry for `cloudflare/downloads/**`; reword the `build.yml` dispatchable entry's `use_when` to `The org image, the SBOM and the release assets`. Remove the matching lines from .claude/hooks/session-start.sh, the `/cut-release` command text and the steward skill that mention the DMG, .pkg, .mcpb, installer or .deb, and the pull_request_template checkboxes for the shim and downloads Worker.
      4. .github/workflows/tests.yml, job `test` only: delete the `setup-node` step and its cache on `mcpb/shim/package-lock.json`, the steps "Install shim dependencies", "Run shim test suite", "Type-check shim" and "Build shim", and the `PRIVACYFENCE_REQUIRE_NODE` env; in `test-python-compat` delete the `--ignore=tests/integration/test_shim_mcp_contract.py` argument and the comment above it. Keep the `ai_client` `npm ci` step.
      5. .gitignore: delete the `*.mcpb` line and the comment naming `mcpb/shim, cloudflare/downloads`.
      6. scripts/build_org_bundle.py is not deleted here (the release phase does it).
      7. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence.
      Stop with status=blocked if a surviving test or script imports something from `mcpb/`, `installer/` or `scripts/pyinstaller_common.py`.
    acceptance:
      - ls mcpb installer debian PrivacyFenceApp.spec privacyfence.service fails with no such file
      - ls scripts/build_dmg.sh scripts/build_deb.sh scripts/build_mcpb.sh fails with no such file
      - grep -n "mcpb" .tld/toolkit.yaml .claude/hooks/session-start.sh prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p15b-build-workflow
    title: Trim build.yml, the R2 script and the release checks
    depends_on: [p15a-packaging, p18a-website-build]
    complexity: M
    touches:
      - .github/workflows/build.yml
      - .github/workflows/publish-pypi.yml
      - scripts/r2_release.py
      - scripts/pre_release_check.py
      - scripts/build_org_bundle.py
      - tests/unit/test_build_org_bundle.py
      - docs/adr/0152-*.md
      - tests/unit/test_r2_release.py
      - tests/unit/test_release_assets.py
      - tests/unit/test_pre_release_check.py
      - tests/unit/test_docs_configuration_reference.py
      - docs/configuration-reference.md
    brief: |
      Read first: docs/1096/plan.md section 3.6; adr/0288-releases-build-the-org-image-the-package-and-the-sbom-only.md; .github/workflows/build.yml (jobs `build` about lines 42-356, `build-windows` 358-591, `build-deb` 593-741, `sbom` 882-970, `finalize-release` 1013-1182); scripts/r2_release.py (`_INSTALLERS` about line 152, `finalize` about 422-489); tests/unit/test_release_assets.py.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. build.yml: delete the jobs `build`, `build-windows`, `build-deb`. First move out of them: the step "Run org-mode Ubuntu release smoke test" (in `build-deb`, about line 650) into `build-org-image` (same command, same environment), and the R2 upload of scripts/sync_room_directory.py, scripts/sign_connector_config.py and the connector-config page (`cp website/tools/connector-config/index.html dist/...`; from the step "Publish release artifacts to Cloudflare R2" of `build`, about lines 332-350) into `finalize-release`. Do not upload scripts/build_org_bundle.py.
      2. `sbom`: delete the `setup-node` step, "Generate Node shim SBOM", `sbom-shim-npm.cdx.json` from the artifact paths and the R2 upload arguments; fix the stale comments about lines 877-881 and 935-939.
      3. `finalize-release`: `needs: [sbom, build-org-image]`; remove `actions: read`; delete the graphical-coverage step if still present; in "Attach release assets" delete the globs `PrivacyFence-*.dmg`, `PrivacyFence.mcpb`, `PrivacyFence-*-setup.exe`, `privacyfence_*.deb`, `sbom-shim-npm.cdx.json` and `scripts/build_org_bundle.py`; delete "Publish release manifest and promote to latest" and "Smoke-test the published download routes (HEAD)"; keep the sync plugin zip, the connector-config page, the pre-release notes step and the `stable` branch step last; rewrite the header comments about lines 972-1012 and the "Download build artifacts" comment so they name only what exists.
      4. scripts/r2_release.py: delete `_INSTALLERS`, `REQUIRED_ARTIFACT_IDS`, `classify_installer`, `build_manifest`, `verify`, `promote`, `finalize` and the manifest and `latest` paths; keep `channel`, `check-tag`, `upload`, `sha256_file`, `_r2_client`, `_head`, `_stored_sha256` and the module docstring (rewritten). Edit tests/unit/test_r2_release.py to the kept functions.
      5. scripts/build_org_bundle.py: delete it (it refuses `--mode org` and is desktop-only) together with tests/unit/test_build_org_bundle.py, and turn the link to it in docs/adr/0152 (about line 51) into plain text (docs/adr/README.md rule 2). Edit tests/unit/test_release_assets.py (it hard-codes `jobs["build"]` and asserts build_org_bundle at about lines 35-37) so it reads the `finalize-release` job and asserts the assets that remain; edit tests/unit/test_docs_configuration_reference.py and docs/configuration-reference.md to drop what they parse from build_org_bundle.py (grep both for it). Organization mode keeps reading a legacy `org/org_bundle.json`; do not touch that code.
      6. scripts/pre_release_check.py: delete the "shim npm test" and "shim typecheck" checks (about lines 65-68) and fix tests/unit/test_pre_release_check.py.
      7. .github/workflows/publish-pypi.yml: fix the comments about lines 3-11, 70-73, 113 and 175 that name the DMG, notarization, build_dmg/deb/installer; keep `wait_for_build` and shorten its timeout to 30 minutes.
      8. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, and python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/build.yml'))".
      Stop with status=blocked if the sbom or build-org-image job needs an artifact produced by a deleted job.
    acceptance:
      - grep -n "build-windows\|build-deb\|PrivacyFence-dmg\|PrivacyFence-mcpb\|sbom-shim-npm" .github/workflows/build.yml prints nothing
      - grep -n "classify_installer\|_INSTALLERS\|def finalize" scripts/r2_release.py prints nothing
      - ls scripts/build_org_bundle.py fails with no such file
      - python3 -m pytest tests/unit/test_r2_release.py tests/unit/test_release_assets.py tests/unit/test_pre_release_check.py tests/unit/test_docs_configuration_reference.py -q passes
      - python3 -m pytest tests/unit -q passes

  - id: p15c-retire-endpoints
    title: Delete the download Worker and the MCP registry publishing
    depends_on: [p15b-build-workflow]
    complexity: S
    touches:
      - cloudflare/downloads/**
      - .github/workflows/deploy-download-worker.yml
      - .github/workflows/publish-mcp-registry.yml
      - scripts/mcp_registry_server_json.py
      - tests/unit/test_mcp_registry_server_json.py
      - .github/workflows/dependency-audit.yml
      - .github/dependabot.yml
    brief: |
      Read first: docs/1096/plan.md section 3.6; .github/workflows/dependency-audit.yml; .github/dependabot.yml.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Delete cloudflare/downloads/ (src, test, migrations, wrangler.toml, package files), .github/workflows/deploy-download-worker.yml, .github/workflows/publish-mcp-registry.yml, scripts/mcp_registry_server_json.py and tests/unit/test_mcp_registry_server_json.py. `cloudflare/oauth-relay/` stays.
      2. .github/dependabot.yml: delete the blocks for `/mcpb/shim` (npm, about lines 43-44) and `/cloudflare/downloads` (about lines 5-6 and 51-60); keep every other block.
      3. .github/workflows/dependency-audit.yml: delete the `npm-audit` job (about lines 200-222), the `npm-audit-worker` job (about lines 224-249), the `mcpb/shim` and `cloudflare/downloads/package*.json` path filters (about lines 55-58 and 67-70) and the prose about the Worker (about lines 33-43).
      4. Run `grep -rln "downloads-worker\|CF_DOWNLOADS_WORKER\|mcp_registry_server_json\|publish-mcp-registry" .github scripts tests` and fix each hit that is in a file of `touches`; list any other hit in your final report (the docs phases handle docs).
      5. Run ruff check ., python3 -m pytest tests/unit -q, and python3 -c "import yaml; [yaml.safe_load(open(p)) for p in ('.github/workflows/dependency-audit.yml', '.github/dependabot.yml')]".
      Stop with status=blocked if a workflow other than the two deleted ones uses the `downloads-worker` environment.
    acceptance:
      - ls cloudflare/downloads fails with no such file
      - ls .github/workflows/deploy-download-worker.yml .github/workflows/publish-mcp-registry.yml fails with no such file
      - grep -rn "cloudflare/downloads\|mcpb/shim" .github prints nothing
      - python3 -m pytest tests/unit -q passes

  - id: p16-ci-legs
    title: Drop the Windows and macOS test legs and their coverage floors
    depends_on: [p15a-packaging]
    complexity: S
    touches:
      - .github/workflows/tests.yml
      - scripts/update_branch_protection.py
      - scripts/check_coverage_floor.py
      - tests/unit/test_update_branch_protection.py
      - tests/unit/test_check_coverage_floor.py
    brief: |
      Read first: docs/1096/plan.md section 3.6; adr/0289-linux-is-the-only-ci-platform.md; scripts/update_branch_protection.py (`REQUIRED_STATUS_CHECKS` about lines 106-107, the comment about line 90); scripts/check_coverage_floor.py (`WINDOWS_MODULE_FLOORS` about lines 246-249, `--platform` about 303-306 and 324-326, the floors of deleted modules about lines 138, 153, 199, 212, 228).
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. tests.yml: delete the jobs `platform-windows` (about lines 267-310, including its `--platform windows` coverage step and the `platform-windows-coverage` artifact) and `platform-macos` (about lines 322-350).
      2. scripts/update_branch_protection.py: remove `platform-windows` and `platform-macos` from `REQUIRED_STATUS_CHECKS`; update tests/unit/test_update_branch_protection.py (about line 359) and fix any comment that names the graphical-session gate.
      3. scripts/check_coverage_floor.py: delete `WINDOWS_MODULE_FLOORS` and the `--platform` option with the docstring text about it, and delete the floor of any module that no longer exists (earlier phases removed the ones they deleted: check every key with `ls src/privacyfence/<module>.py` and delete the missing ones). Leave `OVERALL_FLOOR` and the other floors unchanged (re-measured in the last code phase). Edit tests/unit/test_check_coverage_floor.py to match.
      4. Run ruff check ., python3 -m pytest tests/unit/test_update_branch_protection.py tests/unit/test_check_coverage_floor.py -q, python3 -m pytest tests/unit -q.
      Stop with status=blocked if tests/unit/test_update_branch_protection.py asserts every required check exists in tests.yml and still fails after step 2.
    acceptance:
      - grep -n "platform-windows\|platform-macos" .github/workflows/tests.yml scripts/update_branch_protection.py prints nothing
      - grep -n "WINDOWS_MODULE_FLOORS\|--platform" scripts/check_coverage_floor.py prints nothing
      - python3 -m pytest tests/unit/test_update_branch_protection.py tests/unit/test_check_coverage_floor.py -q passes
      - python3 -m pytest tests/unit -q passes

  - id: p17-deps
    title: Remove the desktop dependencies and regenerate the lockfiles
    depends_on: [p15a-packaging, p14d-mode-leftovers]
    complexity: S
    touches:
      - pyproject.toml
      - requirements/*.lock.txt
      - requirements/README.md
    brief: |
      Read first: docs/1096/plan.md section 3.6; pyproject.toml; requirements/README.md; scripts/update_dependency_locks.sh.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Before editing, run `grep -rn "PIL\|pystray\|pyinstaller" src scripts tests --include=*.py`; if scripts/qa_readme_screenshots.py or scripts/render_ui_review.py (or any file that stays) imports PIL, keep `Pillow` in the `dev` extra only and delete it from the runtime dependencies; otherwise delete both entries.
      2. pyproject.toml: delete the `pystray` and runtime `Pillow` dependencies (the `sys_platform == 'darwin' or 'win32'` markers, about lines 162-170), `pyinstaller>=6.0` in the dev extra (about line 217), the `Download = "https://privacyfence.eu/download/"` project URL (replace it with nothing), the classifiers `Operating System :: MacOS`, `Operating System :: Microsoft :: Windows` and `Intended Audience :: End Users/Desktop` (keep the Linux classifier).
      3. If `uv` is missing run `python3 -m pip install uv`. Run scripts/update_dependency_locks.sh (needs the package index) and commit the resulting requirements/*.lock.txt. `pywin32` stays if the lock still resolves it as a transitive dependency of `mcp`.
      4. requirements/README.md: remove the lines (about 16 and 25) that name pyinstaller, the macOS graphical-session workflow and desktop packaging.
      5. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, python3 -m pip install -e . --dry-run.
      Stop with status=blocked if `uv` or the package index is unreachable: report the exact command and error, and leave the lockfiles untouched.
    acceptance:
      - grep -n "pystray\|pyinstaller\|privacyfence-companion" pyproject.toml requirements/runtime.lock.txt requirements/dev.lock.txt prints nothing
      - python3 -m pytest tests/unit -q passes
      - python3 -m mypy src/privacyfence passes

  - id: p18a-website-build
    title: Retire the download and releases pages, the counter and the release-stats script
    depends_on: [p00-adrs-new]
    complexity: M
    touches:
      - website/download/**
      - website/releases/**
      - website/stats.js
      - website/index.html
      - website/_partials/footer.html
      - scripts/build_site.py
      - scripts/site_security_headers.py
      - scripts/release_stats.py
      - .github/workflows/pages.yml
      - .github/workflows/website-build.yml
      - tests/website_site.py
      - tests/unit/test_build_site.py
      - tests/unit/test_release_stats.py
      - tests/unit/test_website_download_cta.py
      - tests/unit/test_website_releases_page.py
      - tests/unit/test_website_security_headers.py
      - tests/unit/test_website_pages.py
      - tests/unit/test_website_links.py
      - tests/integration/test_download_page.py
      - tests/integration/test_releases_page.py
      - tests/integration/test_website_consent.py
    brief: |
      Read first: docs/1096/plan.md section 3.7; the ADR file in docs/adr whose name starts 0287-; docs/adr/0277-the-websites-primary-call-to-action-is-install.md; scripts/build_site.py (PAGES about lines 97-126, STATIC 146-149, RELEASES_API/ALL_RELEASES_API/HISTORY_API/DOWNLOAD_ORIGIN 89-92, PLATFORMS 219-228, `render_download_cards`, `prerender_download`, `fetch_stable_manifest` 446-523, the /releases/ pre-render 526-665, call sites about 1338-1370, `--release-manifest` about 1457-1461, the `softwareVersion` JSON-LD); website/_partials/footer.html line 14; scripts/site_security_headers.py line 34.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Replace website/download/index.html by a static stub page (same partials, title "Download", one paragraph and two links: PrivacyFence runs as the Docker image or an organization server, link /install/; installers of PrivacyFence 7 remain on https://github.com/privacyfence/privacyfence/releases) and delete website/download/download.js. Delete website/releases/ and the download-counter code of website/stats.js (delete the file if nothing else is in it; keep `site.js`).
      2. scripts/build_site.py: delete `/releases/` from PAGES, the pre-render and manifest functions listed above, the constants, `--release-manifest`, the `softwareVersion` injection and the STATIC entries for the deleted scripts; update the module docstring. Doc sets are not touched here (a later phase edits them).
      3. website/_partials/footer.html: delete the `/releases/` link. scripts/site_security_headers.py: delete `https://downloads.privacyfence.eu` from `connect-src`. website/index.html: delete the `downloadUrl` and `operatingSystem` JSON-LD properties and the download-count statistic with its `stats.js` reference (about lines 39 and 204).
      4. scripts/release_stats.py and tests/unit/test_release_stats.py: delete both (the script imports `classify_installer`, which a later phase deletes). .github/workflows/pages.yml: delete the "Add release stats" step, the `scripts/r2_release.py` path trigger and the comment naming the download Worker. .github/workflows/website-build.yml: delete the two "pre-rendered /download/" and "/releases/" steps and `tests/integration/test_download_page.py` and `test_releases_page.py` from the guardrails pytest call.
      5. Tests: delete tests/unit/test_website_releases_page.py, tests/integration/test_download_page.py, tests/integration/test_releases_page.py; rewrite tests/unit/test_website_download_cta.py so it keeps the call-to-action guardrail (every CTA points at /install/) and drops download-page assertions; edit tests/website_site.py, tests/unit/test_build_site.py, test_website_pages.py, test_website_links.py, test_website_security_headers.py and tests/integration/test_website_consent.py to the new page set (do not delete tests/website_site.py: about 30 test modules import it).
      6. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m pytest tests/integration/test_install_page.py -q (skip reports allowed when Chromium cannot launch; say so), the exact build command of the `build` step in .github/workflows/website-build.yml, and confirm exit 0.
      Stop with status=blocked if the site build fails its link check for a reason other than a page the other website phases still have to rewrite (name the page; links to docs that exist only after v8.0.0 use `DOCS_PENDING_RELEASE` as `scripts/build_site.py` documents, links to docs this change deletes are replaced by /install/).
    acceptance:
      - ls website/releases fails with no such file
      - grep -n "classify_installer\|release_stats" .github/workflows/pages.yml scripts/build_site.py prints nothing
      - python3 -m pytest tests/unit -q passes
      - the build command of the build step in .github/workflows/website-build.yml exits 0

  - id: p18b-website-copy
    title: Rewrite the website pages that describe the desktop apps
    depends_on: [p18a-website-build]
    complexity: M
    touches:
      - website/enterprise/index.html
      - website/faq/index.html
      - website/security/index.html
      - website/privacy/index.html
      - website/gdpr/index.html
      - website/how-it-works/index.html
      - website/connectors/telegram/index.html
      - website/assets/architecture.svg
      - website/assets/architecture-narrow.svg
      - website/styles.css
    brief: |
      Read first: docs/1096/plan.md section 3.7; `grep -rniE "dmg|\.pkg|\.deb|installer|mcpb|desktop mode|companion|local mode" website/enterprise website/faq website/security website/privacy website/gdpr website/how-it-works website/connectors/telegram website/assets website/styles.css`.
      No plan section, phase id or issue number goes into pages; cite ADR numbers only in contributor docs.
      1. In each page of `touches` rewrite every sentence that names the desktop installers, the DMG, `.pkg`, `.deb`, the `.mcpb` extension, "desktop mode", the companion app or "local mode": say that PrivacyFence runs as the Docker image (single-user, on one computer) or as an organization server, and link `/install/`. Do not link to a doc that a later docs phase deletes (install-macos, install-windows, install-linux, platform-support, packaging); link `/install/` instead.
      2. website/assets/architecture.svg and architecture-narrow.svg: delete the text and shapes that label the extension and the companion; keep the AI client, the PrivacyFence server, the approver and the services. website/styles.css: delete the rules whose selectors only the deleted download and releases pages used (grep each class name in website/ before deleting).
      3. Run ruff check ., python3 -m pytest tests/unit/test_website_pages.py tests/unit/test_website_links.py tests/unit/test_build_site.py -q, the exact build command of the `build` step in .github/workflows/website-build.yml.
      Stop with status=blocked if a sentence describes behaviour you cannot match to the code or to docs/org-mode-docker.md: name the page and the sentence.
    acceptance:
      - grep -rliE "\.dmg|\.pkg|\.deb|mcpb|desktop mode|companion" website/enterprise website/faq website/security website/privacy website/gdpr website/how-it-works website/connectors/telegram website/assets website/styles.css prints nothing
      - python3 -m pytest tests/unit -q passes
      - the build command of the build step in .github/workflows/website-build.yml exits 0

  - id: p18c-website-agents
    title: Rewrite the AI agent pages for the custom-connector and Docker path
    depends_on: [p18a-website-build]
    complexity: S
    touches:
      - website/ai-agents/**
      - website/_data/clients.json
      - tests/unit/test_website_agent_pages.py
    brief: |
      Read first: docs/1096/plan.md section 3.7; docs/adr/0098-each-ai-agent-has-its-own-setup-doc-and-website-page.md; `grep -rniE "dmg|\.pkg|\.deb|installer|mcpb|desktop mode|companion|local mode|extension" website/ai-agents website/_data/clients.json`.
      No plan section, phase id or issue number goes into pages; cite ADR numbers only in contributor docs.
      1. website/ai-agents/claude-desktop, chatgpt, claude-code and the index page: rewrite every sentence about the extension, desktop mode, the companion or local mode to the custom-connector and Docker path (link `/install/`). Every client in website/_data/clients.json keeps its page; edit its fields only where they name the extension or desktop mode.
      2. Do not link to docs a later phase deletes (install-macos, install-windows, install-linux, platform-support, packaging).
      3. Run ruff check ., python3 -m pytest tests/unit/test_website_agent_pages.py tests/unit/test_website_links.py -q and the exact build command of the `build` step in .github/workflows/website-build.yml.
      Stop with status=blocked if tests/unit/test_website_agent_pages.py needs a page or field this phase does not create.
    acceptance:
      - grep -rliE "\.dmg|\.pkg|\.deb|mcpb|desktop mode|companion" website/ai-agents website/_data/clients.json prints nothing
      - python3 -m pytest tests/unit/test_website_agent_pages.py tests/unit/test_website_links.py -q passes
      - the build command of the build step in .github/workflows/website-build.yml exits 0

  - id: p19-docs-platform
    title: Delete the install and packaging docs and keep the release-archive sections
    depends_on: [p18b-website-copy, p18c-website-agents, p15c-retire-endpoints, p14d-mode-leftovers]
    complexity: M
    touches:
      - docs/install-*.md
      - docs/platform-support*
      - docs/packaging*
      - docs/downloads-and-release-kpi*
      - docs/release-archive*
      - docs/org-mode-docker.md
      - docs/releasing.md
      - docs/oauth-relay.md
      - docs/README.md
      - cloudflare/oauth-relay/worker-configuration.d.ts
      - scripts/build_site.py
      - tests/unit/test_docs_platform_pages.py
      - tests/unit/test_minimum_os_versions.py
      - tests/unit/test_docs_no_history.py
      - tests/unit/test_website_docs_pages.py
      - tests/unit/test_catalog_server.py
      - tests/integration/test_browser_smoke.py
      - tests/integration/test_claude_code_contract.py
      - scripts/pre_release_check.py
      - scripts/r2_release.py
    brief: |
      Read first: docs/1096/plan.md section 3.7; docs/README.md; downloads-and-release-kpi.md (the sections "Release archive (R2)", "Cloudflare resources", "Credentials / account-owned tokens"); packaging.md lines about 306-348 (Glama listing, organization Docker image, CI); tests/unit/test_docs_references_exist.py.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. In the docs directory `git mv` the file downloads-and-release-kpi.md to release-archive.md; keep its sections "Release archive (R2)", "Cloudflare resources" and "Credentials / account-owned tokens" (edited so nothing mentions the download Worker as live: say it is retired), delete the download-count, KPI and Worker sections. Fix every link to the old name: docs/oauth-relay.md (about lines 33 and 42), docs/releasing.md (about lines 307-316), docs/README.md, scripts/build_site.py `CONTRIBUTOR_DOCS`, cloudflare/oauth-relay/worker-configuration.d.ts (line 2), and every other hit of `grep -rn "downloads-and-release-kpi" . --include=*` outside docs/adr.
      2. Delete install-macos.md, install-windows.md, install-linux.md, platform-support.md, packaging.md; before deleting packaging.md move its "Organization Docker image" section into docs/org-mode-docker.md and its CI and Glama sections into docs/releasing.md (rewritten for what the pipeline builds after this change: the org image, the package, the SBOM). Delete tests/unit/test_docs_platform_pages.py and tests/unit/test_minimum_os_versions.py.
      3. docs/README.md: remove the rows for the five deleted docs and the renamed one's old name, add the row for release-archive.md in the same format as its neighbours. scripts/build_site.py: remove the deleted docs from `CONTRIBUTOR_DOCS` and any published list; tests/unit/test_website_docs_pages.py (about lines 87-91 asserts `content_group("install-macos") == "platform"`): change it to a doc that still exists. tests/unit/test_docs_no_history.py: remove the deleted names from PUBLISHED_DOCS and CONTRIBUTOR_DOCS.
      4. Fix every code comment, docstring, test skip reason and workflow comment that cites a deleted doc: run `python3 -m pytest tests/unit/test_docs_references_exist.py -q` and fix each reported file (the citations are of `platform-support.md`, `packaging.md`, `install-macos.md`, `install-windows.md`, `install-linux.md` and `downloads-and-release-kpi.md`; `release-testing.md` is not deleted); reword the comment to say what it needs without the citation. These edits are comment-only; if a file needs a code change, stop with status=blocked.
      5. Fix the two ADR markdown links to platform-support.md and packaging.md that test_docs_links reports (a link whose target is deleted may be changed to plain text; the ADR body is otherwise frozen).
      6. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_allowlist.py tests/unit/test_website_docs_pages.py tests/unit/test_readme_site_links.py -q, ruff check ., python3 -m pytest tests/unit -q.
      Stop with status=blocked if a published website page links to a deleted doc.
    acceptance:
      - ls install-macos.md install-windows.md install-linux.md platform-support.md packaging.md downloads-and-release-kpi.md fails with no such file
      - ls release-archive.md lists the file
      - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_allowlist.py tests/unit/test_website_docs_pages.py -q passes
      - python3 -m pytest tests/unit -q passes

  - id: p20-docs-user
    title: Rewrite the user and operator docs for an organization-only product
    depends_on: [p19-docs-platform]
    complexity: M
    touches:
      - README.md
      - SECURITY.md
      - docs/README.md
      - docs/getting-started.md
      - docs/how-it-works.md
      - docs/security-and-compliance.md
      - docs/configuration-reference.md
      - docs/approvals-and-policy.md
      - docs/connecting-a-service.md
      - docs/connect-claude-code.md
      - docs/connect-claude-desktop.md
      - docs/connect-claude-ai.md
      - docs/connect-chatgpt.md
      - docs/connect-gemini-enterprise.md
      - docs/moving-to-organization-mode.md
      - docs/org-mode-setup-guide.md
      - docs/telegram-setup.md
      - docs/privacyfence-sync.md
      - docs/connector-qa.md
      - docs/peek-setup.md
      - docs/oidc-provider.md
      - docs/salesforce-setup.md
      - docs/google-cloud-setup.md
      - docs/grist-setup.md
      - docs/atlassian-setup.md
      - docs/ai-client-qa.md
      - src/privacyfence/resources/settings.yaml.example
      - sync/README.md
      - plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md
    brief: |
      Read first: docs/1096/plan.md sections 3.3, 3.4, 3.5 and 3.7; docs/README.md; docs/moving-to-organization-mode.md; docs/coding-and-testing-guidelines.md section on docs (the no-history rule: the words "no longer", "formerly", "used to" and "now" are banned in published docs by tests/unit/test_docs_no_history.py).
      No plan section, phase id or issue number goes into docs; cite ADR 0283-0290 by number only in contributor docs.
      1. docs/moving-to-organization-mode.md: rewrite from "deprecated, will be removed" to "PrivacyFence 8 runs only in organization mode": what a desktop install is, what happens when 8 starts on one (quote the two refusal texts of plan section 3.3 and 3.4), the two ways forward (single-user Docker; an organization server), bringing rules along, and "Before upgrading to 8.x" (install the latest 7.x, start it once, check the Admin line). Keep the "Going back to 6.x" section if its content is still true; delete what describes the strip, the notification and the status notice. Do not add anchors the docs site cannot serve before the 8.0.0 tag.
      2. docs/connect-claude-desktop.md: delete "Local mode", the extension and `.mcpb` references and the "Files, local mode" part; keep the custom-connector and Docker sections. docs/connect-claude-code.md and docs/connect-chatgpt.md: delete the "Local mode" sections, the companion token steps, the "Files" paragraph about the shim (files go through upload slots, ADR 0286) and the local-mode troubleshooting rows. docs/connect-claude-ai.md: delete the "Local mode: not available" lines. docs/connect-gemini-enterprise.md: fix any install-* link.
      3. docs/getting-started.md ("What you need", "Pick your platform", "Local or organization deployment"), docs/how-it-works.md ("The pieces", "Opening PrivacyFence opens Approvals", "Local mode and organization mode", "Files", "Which AI system is asking"), docs/security-and-compliance.md ("Deployment modes", "Local-mode trust boundary", "Privilege separation", local-install paragraphs of passkeys and recovery), docs/configuration-reference.md (local-mode keys, `--print-mcp-token`, companion; keep it matching settings.yaml.example because tests/unit/test_docs_configuration_reference.py parses it), docs/approvals-and-policy.md, docs/connecting-a-service.md, docs/telegram-setup.md, docs/ai-client-qa.md, docs/privacyfence-sync.md ("Local mode" section): describe organization mode and single-user Docker only.
      4. Minor edits: the install-* and platform-support links in docs/org-mode-setup-guide.md (5 places; keep "Moving a person from a desktop install"; delete the "For IT admins" bundle sections), connector-qa.md, peek-setup.md, oidc-provider.md, salesforce-setup.md, google-cloud-setup.md, grist-setup.md, atlassian-setup.md.
      5. README.md: the lines naming the OS table with `.dmg`, `-setup.exe` and `.deb` (about lines 50-56), the install steps (about 87-97), the extension and diagram caption text (about 11, 13, 42-44), "ChatGPT desktop on macOS" (about 59) and "in local mode the name" (about 117). README.md is the PyPI long description: every link must resolve and tests/unit/test_readme_site_links.py must pass. SECURITY.md: delete the companion app and `.mcpb` shim from the in-scope list (about lines 22-23). docs/README.md: update the section intros that say "in local mode and in an organization deployment" and the `connect-claude-desktop` row text.
      6. sync/README.md and its byte-identical copy plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md: delete the "Local mode (PrivacyFence on your own computer)" section and the lines about the companion, macOS Keychain, launchd and desktop installs (about lines 54, 134, 256, 746, 900-992); `DEFAULT_SERVER` stays. Edit both files identically, then `diff sync/README.md plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md` must print nothing.
      7. src/privacyfence/resources/settings.yaml.example: delete the comments and keys that apply only to desktop mode (`grep -nE "companion|desktop|local mode|privilege" src/privacyfence/resources/settings.yaml.example`), and keep docs/configuration-reference.md in step with it (tests/unit/test_docs_configuration_reference.py parses both). Never delete a key that code under src/privacyfence still reads (grep the key name first).
      8. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_docs_configuration_reference.py tests/unit/test_readme_site_links.py tests/unit/test_website_agent_pages.py tests/unit/test_website_links.py -q, then python3 -m pytest tests/unit -q and, in sync/, the sync tests as `.tld/toolkit.yaml` verify.dod_conditional says only if sync/ source changed (it did not: skip).
      Stop with status=blocked if a doc describes behaviour you cannot confirm from the code (name the doc and the sentence) instead of guessing, or if a settings.yaml.example key you would delete is still read by code.
    acceptance:
      - grep -rln "install-macos\|install-windows\|install-linux\|platform-support\|\.mcpb\|companion app" README.md docs sync plugins SECURITY.md --include=*.md prints nothing except files under docs/adr
      - diff sync/README.md plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md prints nothing
      - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_docs_configuration_reference.py tests/unit/test_readme_site_links.py -q passes
      - python3 -m pytest tests/unit -q passes

  - id: p21-docs-contributor
    title: Rewrite the contributor docs for the new pipeline
    depends_on: [p19-docs-platform]
    complexity: M
    touches:
      - CLAUDE.md
      - AGENTS.md
      - CONTRIBUTING.md
      - docs/testing-policy.md
      - docs/releasing.md
      - docs/coding-and-testing-guidelines.md
      - docs/release-testing.md
      - docs/dev-vs-live-setup.md
      - docs/release-archive*
    brief: |
      Read first: docs/1096/plan.md sections 3.6 and 3.7; docs/releasing.md; docs/testing-policy.md; docs/coding-and-testing-guidelines.md (sections 1.9, 2.1, 2.7, 2.12, 3, 4); CONTRIBUTING.md.
      No plan section, phase id or issue number goes into docs; cite ADR 0283-0290 by number.
      1. docs/testing-policy.md: rewrite the seven-layer table (layer 3 runs on Linux only; layer 6 "Packaged artifact" becomes the Docker image and Ubuntu release smoke tests), delete the `platform-windows` and `platform-macos` rows, the shim steps of the `test` row, the `--ignore` note of `test-python-compat`, and "Layer 6: packaged artifact" (about lines 265-296) except what still applies to the org image.
      2. docs/releasing.md: rewrite "Pre-flight `build.yml` itself" (mentions of build, build-windows, build-deb), "Packaged-artifact release gating" (the `needs:` list becomes `[sbom, build-org-image]`), delete "Who can download a pre-release" (the Worker), update the Cloudflare R2 section to say R2 keeps uploads and no manifest, and keep its links to release-archive.md valid.
      3. docs/coding-and-testing-guidelines.md: update sections 1.9, 2.1 (the table of test locations), 2.7 (the definition of done: it must match `verify.dod` and `dod_conditional` of .tld/toolkit.yaml after the packaging phase: no npm shim lines, no cloudflare/downloads line), 2.12 (diagnostics), 3 (the PyInstaller row of adding a connector) and delete section 4 "Packaging changes" (about line 551); delete mentions of the KPI doc and the Worker.
      4. CONTRIBUTING.md: delete `deploy-download-worker.yml` (about line 46), the install-type text (about lines 75 and 81) and the `mcpb/shim/` Node section (about lines 92-93). AGENTS.md (about lines 15, 23-24) and CLAUDE.md (about line 9, the R2 archive line): remove what names `mcpb/`, the DMG or the Worker; keep both files thin.
      5. docs/release-testing.md: delete the per-platform "Human checks" sections (macOS, Windows, Debian/Ubuntu); keep "Every platform" and "Org mode on a phone". docs/dev-vs-live-setup.md: rewrite to the organization and single-user dev setup (a development server next to a live one on the same machine, separate data directories and ports), deleting the packaged-install clash material.
      6. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q and python3 -m pytest tests/unit -q.
      Stop with status=blocked if section 2.7 and .tld/toolkit.yaml disagree after your edit.
    acceptance:
      - grep -rn "platform-windows\|platform-macos\|build-windows\|build-deb\|mcpb\|deploy-download-worker" docs/testing-policy.md docs/releasing.md docs/coding-and-testing-guidelines.md CONTRIBUTING.md AGENTS.md CLAUDE.md prints nothing
      - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q passes
      - python3 -m pytest tests/unit -q passes

  - id: p22-ratchet
    title: Re-measure the coverage floors
    depends_on: [p14d-mode-leftovers, p16-ci-legs, p17-deps]
    complexity: S
    touches:
      - scripts/check_coverage_floor.py
      - tests/unit/test_check_coverage_floor.py
    brief: |
      Read first: scripts/check_coverage_floor.py (`OVERALL_FLOOR`, the per-module floor tables and how it reads coverage.json); docs/coding-and-testing-guidelines.md section 2.7.
      No plan section, phase id or issue number goes into code, tests, comments or docstrings; cite ADR 0283-0290 by number.
      1. Run the full measurement exactly as verify.dod does: pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json, then python3 scripts/check_coverage_floor.py coverage.json.
      2. For every floor key whose module no longer exists, delete the key. For every module the script reports below its floor, add tests for the missing lines until it passes; never lower a floor to pass.
      3. For `OVERALL_FLOOR` and for every module whose measured coverage is more than 0.5 percentage points above its floor, raise the floor to the measured value rounded down to one decimal (the script's own ratchet convention: read its header comment first).
      4. Edit tests/unit/test_check_coverage_floor.py to the final tables.
      5. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence, bandit -c pyproject.toml -r src, python3 scripts/mypy_strict_modules.py.
      Stop with status=blocked if a module that must stay at 100% (session_auth, secure_files, principal) is below it and the missing line is code you cannot test.
    acceptance:
      - python3 scripts/check_coverage_floor.py coverage.json exits 0 after the full pytest --cov run
      - python3 -m pytest tests/unit/test_check_coverage_floor.py -q passes
      - bandit -c pyproject.toml -r src passes
      - python3 -m mypy src/privacyfence passes

  - id: p23-retirement
    title: Status lines on the old ADRs and the changelog entries
    depends_on: [p20-docs-user, p21-docs-contributor, p22-ratchet]
    complexity: M
    touches:
      - docs/adr/0002-*.md
      - docs/adr/0003-*.md
      - docs/adr/0007-*.md
      - docs/adr/0012-*.md
      - docs/adr/0018-*.md
      - docs/adr/0024-*.md
      - docs/adr/0026-*.md
      - docs/adr/0027-*.md
      - docs/adr/0028-*.md
      - docs/adr/0029-*.md
      - docs/adr/0031-*.md
      - docs/adr/0039-*.md
      - docs/adr/0041-*.md
      - docs/adr/0042-*.md
      - docs/adr/0043-*.md
      - docs/adr/0044-*.md
      - docs/adr/0045-*.md
      - docs/adr/0046-*.md
      - docs/adr/0054-*.md
      - docs/adr/0057-*.md
      - docs/adr/0058-*.md
      - docs/adr/0059-*.md
      - docs/adr/0060-*.md
      - docs/adr/0062-*.md
      - docs/adr/0087-*.md
      - docs/adr/0088-*.md
      - docs/adr/0104-*.md
      - docs/adr/0112-*.md
      - docs/adr/0160-*.md
      - docs/adr/0183-*.md
      - docs/adr/0184-*.md
      - docs/adr/0187-*.md
      - docs/adr/0203-*.md
      - docs/adr/0261-*.md
      - docs/adr/0262-*.md
      - docs/adr/0277-*.md
      - docs/adr/0283-*.md
      - docs/adr/0284-*.md
      - docs/adr/0285-*.md
      - docs/adr/0286-*.md
      - docs/adr/0287-*.md
      - docs/adr/0288-*.md
      - docs/adr/0289-*.md
      - docs/adr/*.md
      - docs/adr/README.md
      - CHANGELOG.md
    brief: |
      Read first: docs/1096/plan.md section 3.8 (the table and the list of old ADRs); docs/adr/README.md (the supersession convention and the Index table); docs/adr/0087-*.md and its index row (the example of a Status line that points forward); CHANGELOG.md lines 1-60 and the `### Removed` entries of 7.0.0.
      No plan section, phase id or issue number goes into an ADR or the changelog.
      1. For each old ADR listed in plan section 3.8, change only its `## Status` section: keep the existing text and append the forward reference as the line `**Superseded by ADR 0283, <date>.**` (a Markdown link to the new ADR file, relative to the old ADR's own directory) (or `Deprecated:`/`Amended by` as the list says; the exact words and target numbers per ADR are in plan section 3.8). Never edit another section of an old ADR. Update that ADR's row in the Index table of docs/adr/README.md (Status column) in the same style as 0087's row.
      2. In the Status line of each of ADRs 0283-0290 append the sentence `Implemented.` and change nothing else in them; the bodies, including their Verification sections, were written final by the first phase.
      3. `grep -rliE "companion|mcpb|privilege|installer|DMG" docs/adr` and for every ADR it lists that is not in plan section 3.8's list, read its Decision section: if it describes behaviour that exists only in desktop mode (an installer, the companion, privilege separation, the control channel, the extension, the file bridge), append `Deprecated: ADR 0283 removed desktop mode` to its Status and Index row; if it only mentions a desktop term in passing, leave it. Report the added numbers in your final message.
      4. CHANGELOG.md, under `## [Unreleased]`, add a `### Removed` block (and a `### Changed` block for the three behaviour changes) of entries in the file's `- **Bold sentence.** Detail.` style: the macOS, Windows and Linux installers and the companion are gone; the Claude Desktop extension (`.mcpb`) and its MCP registry entry are no longer published; the download page and Worker are retired (old installers remain on their GitHub Releases); `PRIVACYFENCE_MODE=local` and a bundle `"mode": "local"` refuse to start; 8 refuses a data directory that has desktop traces or no 7.x marker (quote the two texts' first sentences); tools no longer take `destination_dir` or a plain `local_path` and attachments take `upload:<id>` only (start this entry with `**Breaking:`); `privacyfence_status` no longer carries `deprecation` or `next_step: open_privacyfence_companion` (`**Breaking:`); `privacyfence-companion` is removed; Windows and macOS test legs dropped from CI is internal and gets no entry. Open no version heading.
      5. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q, python3 -m pytest tests/unit -q (the changelog parser tests included), ruff check ..
      Stop with status=blocked if an ADR Status section has no `## Status` heading in the form the README describes.
    acceptance:
      - grep -c "Superseded by \[ADR 0283\]" docs/adr/0002-*.md docs/adr/0003-*.md docs/adr/0261-*.md prints 1 for each file
      - grep -n "^## \[" CHANGELOG.md | head -3 shows ## [Unreleased] first and no new version heading
      - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q passes
      - python3 -m pytest tests/unit -q passes
```
