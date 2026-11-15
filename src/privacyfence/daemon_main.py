"""PrivacyFence daemon: persistent background process that owns credentials,
connectors, and the embedded web approval/config UI.

Runs as a system service under its own account (the LaunchDaemon
``system/com.privacyfence.daemon`` on macOS, the system unit
``privacyfence-daemon.service`` on Linux, the ``PrivacyFence`` service on
Windows -- see ADR 0003 and ADR 0026). Only one instance is allowed
(enforced via a lock file). Claude reaches this process over the embedded
``/mcp`` Streamable HTTP endpoint (see web/mcp_dispatch.py's module
docstring), the only transport there is; ``connector_host.py``'s
``ConnectorHost`` holds the connectors that endpoint dispatches to. A human
reaches it the same way: over the embedded web approval/settings surfaces
(``/approvals``, and ``/settings`` when ``web.settings.enabled``). There is
no native UI in this process: two approval surfaces would mean two places
for a security fix to land (see ADR 0001), so the web surface is the only
one, on every platform this process runs on.

Threading model:
  - Main thread:   waits on ``_wait_for_shutdown()`` (below) until
    ``request_shutdown()`` is called (the web settings page's "Quit
    PrivacyFence" action) or the process receives SIGINT/Ctrl-C -- there is
    no run loop of its own to host anymore, since every Claude-facing and
    human-facing surface already runs on the web thread below.
  - Web thread:    uvicorn serving the embedded HTTP server (web/server.py)
    that hosts ``/mcp`` and the web approval/settings surfaces -- this is
    also the event loop every connector call now actually runs on.
  - Cache warm:    short-lived background thread(s) started right after the
    web server's event loop is known, refreshing Slack/Telegram directory
    caches if they've gone stale -- see _warm_connector_caches(). Kept off
    the main thread so a large workspace/account doesn't delay startup.
  - Personal data sweep: a background thread erasing Atlassian user-cache
    entries that missed their personal data report, at startup and every
    few hours -- see _run_personal_data_sweep().

Configuration is split into two files (see paths.py):
  - ``org/org_bundle.json``    — organization-level app registrations (Google
    OAuth client, Slack app, Salesforce Connected App, Atlassian OAuth app),
    installed via PrivacyFence Settings (``/settings``, or by hand-editing
    this file). Optional per service; a connector is offered only if its
    section is present. Telegram's api_id/api_hash are the one exception:
    they identify the PrivacyFence app itself (not an organization) and are
    baked into the release build — see app_credentials.py. Also carries
    ``unattended_sessions.enabled`` — a deliberate per-organization opt-in,
    not a per-user setting, so it lives here rather than settings.yaml.
    ``rooms`` (optional) is a static room/resource directory snapshot IT
    refreshes with ``scripts/sync_room_directory.py``, using a separate,
    admin-scoped Google Cloud project — see that script's module docstring
    and docs/google-cloud-setup.md. It's plain data, not a credential, and is
    handed straight to CalendarConnector; the Calendar OAuth client itself
    never carries Workspace-admin directory scope. ``mode``/``server``/
    ``idp`` switch this daemon into org mode — a real OAuth 2.1 authorization
    server on ``/mcp`` instead of the local shared-secret token, human
    identity resolved via the org's own OIDC IdP (see ADR 0011). Absent
    (every install that hasn't opted in) means local mode. See org_mode.py and org_identity.py
    for the schema, or build a bundle with ``scripts/build_org_bundle.py
    --mode org ...``.
  - ``config/settings.yaml``   — per-user settings: privacy policy,
    connectors{enabled}, auto_accept (the policy rules),
    pii_detection{enabled, detect_ip_addresses, detect_financial_figures,
    audit_match_details}, step_up{enabled, scope, rp_id, rp_name,
    require_passkey} (local mode's own WebAuthn passkey enrollment config,
    see step_up_config.py). No secrets live here. Lives under
    ``paths.authority_dir()``, not the user-dir root
    directly -- see that function's own docstring.
Per-user credentials (OAuth tokens, Telegram session) live under
``credentials/``, one file per connector -- ``paths.user_dir()`` itself,
reachable by the agent, since these are its own operational data rather
than the human's authority.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import portalocker
import yaml

from . import (
    __version__,
    admin_config,
    agent_runtimes,
    audit_forwarding,
    connector_configs,
    data_format,
    grist_provisioning,
    oauth_apps,
    oidc_provider,
    org_bundle_signing,
    org_mode,
    paths,
    peek_provisioning,
    policy_upgrade,
    relay_client,
    runtime_credential_sweeper,
    scoped_tokens,
    step_up_config,
    upstream_claims,
    upstream_mcp_config,
    upstream_mcp_registry,
    upstream_mcp_runtime,
)
from .policy import store as policy_store

if TYPE_CHECKING:
    from .approvals import PendingApprovalRegistry
from .paths import (
    LEGACY_ORG_CONFIG_FILENAME,
    ORG_BUNDLE_FILENAME,
    authority_dir,
    authority_root,
    data_dir,
    org_dir,
    safe_principal_id,
    user_dir,
)
from .principal import LOCAL_PRINCIPAL, LOCAL_PRINCIPAL_ID, Principal, current_principal
from .webauthn_stepup import StepUpRequirementChange
from .app_credentials import telegram_app_credentials
from .approval_ui import init_approval_ui
from .audit_log import (
    AuditEntry,
    compute_security_config_hash,
    current_week,
    get_audit_logger,
    init_audit_logger,
)
from .auto_accept import (
    init_config_path,
    set_policy_v2_store_rules,
)
from .pii_detector import init_pii_detection
from .privacy_filter import check_consistency_warnings, init_privacy_filter
from .safe_errors import SecretRedactingFormatter, public_message
from .secure_files import (
    InsecurePermissionsError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    audit_directory_permissions,
)
from .connectors.apps_script import AppsScriptConnector
from .connectors.assets import AssetsConnector
from .connectors.calendar import CalendarConnector
from .connectors.confluence import ConfluenceConnector
from .connectors.contacts import ContactsConnector
from .connectors.drive import DriveConnector
from .connectors.gmail import GmailConnector
from .connectors.grist import GristConnector
from .connectors.jira import JiraConnector
from .connectors.miro import MiroConnector
from .connectors.salesforce import SalesforceConnector
from .connectors.slack import SlackConnector
from .connectors.tasks import TasksConnector
from .connectors.peek import PeekConnector
from .connectors.telegram import TelegramConnector
from .apps_script_client import AppsScriptClient, AppsScriptClientError
from .atlassian_oauth import AtlassianOAuthError, assets_enabled, assets_workspace_override
from .atlassian_users import AtlassianUserDirectory, erase_overdue_caches
from .atlassian_oauth import load_token_file as load_atlassian_token
from .calendar_client import CalendarClient, CalendarClientError
from .assets_client import AssetsClient, AssetsClientError
from .confluence_client import ConfluenceClient, ConfluenceClientError
from .connector_host import ConnectorHost
from .contacts_client import ContactsClient, ContactsClientError
from .drive_client import DriveClient, DriveClientError
from .gmail_client import GmailClient, GmailClientError
from .grist_auth import GristClientError
from .grist_auth import bundle_settings as grist_bundle_settings
from .grist_auth import load_token_file as load_grist_token
from .grist_auth import resolve_credential as grist_resolve_credential
from .grist_client import GristClient
from .jira_client import JiraClient, JiraClientError
from .miro_auth import MiroClientError, MiroTokenProvider
from .miro_auth import bundle_settings as miro_bundle_settings
from .miro_auth import load_token_file as load_miro_token
from .miro_client import MiroClient
from .salesforce_client import DEFAULT_REPORT_MAX_PAGES, SalesforceClient, SalesforceClientError
from .salesforce_client import load_token_file as load_salesforce_token
from .slack_client import SlackClient, SlackClientError
from .peek_client import PeekClient, PeekClientError, PeekProvisioningClient
from .peek_client import bundle_base_url as peek_bundle_base_url
from .peek_client import load_token_file as load_peek_token
from .peek_client import resolve_server as peek_resolve_server
from .slack_client import load_token_file as load_slack_token
from .tasks_client import TasksClient, TasksClientError
from .telegram_client import TelegramClientError, TelegramPrivacyFenceClient

logger = logging.getLogger("privacyfence.daemon")

PROJECT_ROOT = str(data_dir())
LOCK_FILE = os.path.join(PROJECT_ROOT, "privacyfence.lock")

# Where each connector's per-user credential is cached. Purely internal — no
# longer user-configurable, since org app registration and per-user auth are
# now handled separately (see module docstring).
TOKEN_FILES: dict[str, str] = {
    "gmail": "credentials/token.json",
    "drive": "credentials/drive_token.json",
    "calendar": "credentials/calendar_token.json",
    "contacts": "credentials/contacts_token.json",
    "tasks": "credentials/tasks_token.json",
    "apps_script": "credentials/apps_script_token.json",
    "slack": "credentials/slack_token.json",
    "salesforce": "credentials/salesforce_token.json",
    "grist": "credentials/grist_token.json",
    "miro": "credentials/miro_token.json",
    "atlassian": "credentials/atlassian_token.json",
    "telegram": "credentials/telegram.session",
    "peek": "credentials/peek_token.json",
}

_lock_fd: int | None = None


# ---------------------------------------------------------------------------- #
# Instance lock
# ---------------------------------------------------------------------------- #

def _acquire_instance_lock() -> bool:
    # portalocker picks the right OS primitive itself -- fcntl.flock on
    # POSIX, msvcrt/LockFileEx on Windows (which has no fcntl module at
    # all: importing it unconditionally used to crash the daemon at
    # startup on Windows before it got anywhere near this function). Same
    # "don't hand-roll a platform-locking primitive" reasoning pyproject.
    # toml already gives for PyJWT/webauthn. It's handed the raw fd
    # directly (accepted alongside file objects/fileno()-havers), so the
    # rest of this function -- and every caller/test -- is unchanged.
    global _lock_fd
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        portalocker.lock(fd, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except portalocker.exceptions.LockException:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _lock_fd = fd
    return True


def _release_instance_lock() -> None:
    global _lock_fd
    if _lock_fd is not None:
        try:
            portalocker.unlock(_lock_fd)
            os.close(_lock_fd)
        except (OSError, portalocker.exceptions.LockException) as exc:
            # Best-effort release on shutdown -- swallowed deliberately (the
            # process is on its way out either way), but logged so a closed/
            # already-unlocked fd here isn't silently invisible if something
            # about shutdown ordering ever needs debugging.
            logger.debug("Failed to release instance lock cleanly: %s", exc)
        _lock_fd = None


# ---------------------------------------------------------------------------- #
# Shutdown wait: the main thread used to block inside an AppKit menu bar's
# run loop until its "Quit PrivacyFence" ended it. That host is gone, so
# run_app() below now blocks on this plain threading.Event instead -- request_shutdown()
# (the "Quit PrivacyFence" action) sets it, same as SIGINT/Ctrl-C raising KeyboardInterrupt out of the wait.
# ---------------------------------------------------------------------------- #

_shutdown_event = threading.Event()


def request_shutdown() -> None:
    """Signal run_app()'s own wait loop to return. Called by
    the "Quit PrivacyFence" action."""
    _shutdown_event.set()


def _wait_for_shutdown() -> None:
    """Blocks the calling (main) thread until request_shutdown() is called
    or the process receives SIGINT/Ctrl-C (KeyboardInterrupt). Everything
    Claude-facing and human-facing already runs on the web server's own
    thread (McpDispatcher.call() via /mcp, and every web/routes_*.py
    handler) -- this thread has nothing left to do but wait."""
    _shutdown_event.wait()


PERSONAL_DATA_SWEEP_INTERVAL_SECONDS = 6 * 60 * 60


def _run_personal_data_sweep() -> None:
    """Erase the Atlassian user-cache entries that missed their personal data report (ADR 0202)
    once at startup, then every PERSONAL_DATA_SWEEP_INTERVAL_SECONDS until shutdown. It reads the
    cache files themselves, so it also covers organization-server users whose connectors are not
    built and who therefore report nothing."""
    while True:
        try:
            erase_overdue_caches(data_dir())
        except Exception as exc:
            logger.warning("Atlassian personal data sweep failed (will retry): %s", exc)
        if _shutdown_event.wait(PERSONAL_DATA_SWEEP_INTERVAL_SECONDS):
            return


# ---------------------------------------------------------------------------- #
# Configuration & logging
# ---------------------------------------------------------------------------- #

def _resolve_path(path: str) -> str:
    """Relative to ``PROJECT_ROOT`` for the local principal -- including
    for the tests that monkeypatch
    ``PROJECT_ROOT`` directly to sandbox where a test run reads/writes --
    or to that *other* principal's own storage root when this runs
    inside a ``principal_scope()`` block for someone else (only
    connector_registry.py's ``ConnectorRegistry.get()`` does that today).

    For connector OAuth tokens/caches and the daemon's own runtime log --
    the agent's operational data, still reachable under ``user_dir()``.
    ``config/settings.yaml`` does *not* go through this any more -- see
    ``_resolve_authority_path()``.
    """
    if os.path.isabs(path):
        return path
    principal = current_principal()
    if principal.id == LOCAL_PRINCIPAL_ID:
        return os.path.join(PROJECT_ROOT, path)
    return str(user_dir(principal) / path)


def _resolve_authority_path(path: str) -> str:
    """Like ``_resolve_path()``, but rooted at the ``authority`` subtree
    rather than ``user_dir()``/``PROJECT_ROOT`` directly -- the split for the files that back the *human's* authority (today, just
    ``config/settings.yaml``) rather than the agent's own operational data.
    Mirrors ``_resolve_path()``'s own local-vs-other-principal branching,
    including anchoring the local principal on ``PROJECT_ROOT`` rather than
    calling ``user_dir()``/``data_dir()`` itself, for the same test-
    sandboxing reason given in that function's docstring.
    """
    if os.path.isabs(path):
        return path
    principal = current_principal()
    if principal.id == LOCAL_PRINCIPAL_ID:
        return str(authority_root(Path(PROJECT_ROOT)) / path)
    return str(authority_dir(principal) / path)


def _bootstrap_config(resolved: str) -> None:
    """Seed a default settings.yaml from the packaged example on first run.

    The example carries no secrets (org credentials and per-user auth are
    handled separately, via PrivacyFence Settings), so it's safe to install
    automatically now that there's no setup wizard to do it.
    """
    example = Path(__file__).parent / "resources" / "settings.yaml.example"
    atomic_write_bytes(resolved, example.read_bytes())


# Warnings raised before setup_logging() has given the root logger a handler: the local principal's
# load_config() runs first, and a warning logged then would reach no file. setup_logging() logs and
# clears these; a caller that runs after it (_load_principal_settings) flushes them itself.
_deferred_warnings: list[str] = []


def _flush_deferred_warnings() -> None:
    while _deferred_warnings:
        logger.warning("%s", _deferred_warnings.pop(0))


def _drop_converted_v1_sections(config: dict[str, Any], resolved: str) -> None:
    """Remove the v1 policy sections an earlier release already converted, and persist that.

    Some earlier releases converted ``auto_accept_rules``/``auto_accept_grants`` into ``auto_accept:``
    on startup and left the originals on disk, so every install that ran one of them would otherwise
    be refused by ``reject_v1_sections`` below. A failed write is not fatal: the in-memory config is
    already clean, and the next start tries again (ADR 0047).
    """
    removed = policy_store.drop_converted_v1_sections(config)
    if not removed:
        return
    names = ", ".join(removed)
    try:
        atomic_write_text(resolved, yaml.safe_dump(config, default_flow_style=False, allow_unicode=True))
    except OSError as exc:
        _deferred_warnings.append(f"{resolved}: ignoring already-converted {names}; could not rewrite the file: {exc}")
        return
    _deferred_warnings.append(
        f"{resolved}: removed {names}, left behind by an earlier version's conversion to the "
        "auto_accept: section, which already holds those rules"
    )


def load_config(config_path: str, *, principal: Principal | None = None) -> dict[str, Any]:
    resolved = _resolve_path(config_path)
    if not os.path.exists(resolved):
        _bootstrap_config(resolved)
    with open(resolved, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not isinstance(config, dict):
        raise ValueError(f"Config file {resolved} did not parse to a mapping")
    _drop_converted_v1_sections(config, resolved)
    if principal is not None:
        policy_upgrade.ensure_scope_rows(config, resolved, principal, log=_deferred_warnings.append)
        policy_upgrade.ensure_token_rules(config, principal, log=_deferred_warnings.append)
    policy_store.reject_v1_sections(config, resolved)
    return config


# True once any load_org_config() call in this process copied org_config.json
# to org_bundle.json; run_app() then records the org_bundle_adopted audit
# entry. tests/conftest.py::_reset puts it back to False.
_bundle_adopted_this_start = False


def _warn_if_legacy_bundle_changed() -> None:
    """After adoption 7.x ignores org_config.json. An operator who followed 6.x
    docs and copied a new bundle to the old name is told, not met with silence."""
    record = org_bundle_signing.load_adopted_record(org_dir())
    recorded = record.get("sha256")
    legacy = org_dir() / LEGACY_ORG_CONFIG_FILENAME
    if not recorded or not legacy.exists():
        return
    try:
        current = org_bundle_signing.sha256_hex(legacy.read_bytes())
    except OSError:
        return
    if current != recorded:
        logger.warning(
            "org/org_config.json changed after the upgrade and is ignored: PrivacyFence 7 no longer "
            "reads it, and an organization server takes no new bundle. Change settings in the "
            "environment (privacyfence.env) or on the Admin page, and your own connector apps as "
            "signed connector configs (docs/org-mode-setup-guide.md)."
        )


def load_org_config() -> dict[str, Any]:
    """Load the installed organization config bundle.

    Three states, not two: absent entirely → {} (local mode, same
    "missing config → connector skipped" philosophy used for every
    connector below); a valid JSON object → parsed and returned as
    configured; present but broken (unreadable, malformed JSON, or a
    non-object top level) → raise ``org_mode.ConfigurationError`` and
    refuse to start.

    Before this, "broken" collapsed into the same {} result as "absent" —
    so a corrupted or tampered org_config.json silently behaved exactly
    like no org config at all. For an org-mode install that's a silent
    downgrade to no IdP-backed auth at all (org_mode.resolve_mode({})
    resolves to "local"), triggerable by anything that can truncate or
    corrupt the file — not a state this daemon should ever paper over.
    A desktop (local-mode) bundle is installed in PrivacyFence Settings under
    Admin → Organization configuration → "Install configuration file…". An
    organization server takes no new bundle (settings come from the
    environment, its own apps from signed connector configs).

    Bundle signing (ADR 0016): a well-formed-but-hostile *replacement* of
    this file (as opposed to the malformed-file cases above) is a
    separate, more dangerous failure mode the checks above can't catch —
    parses fine, just carries someone else's IdP/app credentials. Every
    load here also runs the bundle through org_bundle_signing.verify_
    and_maybe_pin(): a bundle that fails to verify against a previously
    pinned signing key raises ConfigurationError the same as a malformed
    one, and ``mode: org`` additionally requires the bundle to actually
    be signed at all (see that module's own docstring for the trust-on-
    first-use model). A local-mode install that has never adopted
    signing is unaffected — this call is then a no-op past the "no
    signing key pinned yet and no signature present" pass-through case.
    """
    global _bundle_adopted_this_start
    # Runs several times per process, so only ever set it to True.
    _bundle_adopted_this_start |= org_bundle_signing.adopt_legacy_files(org_dir())
    _warn_if_legacy_bundle_changed()
    path = org_dir() / ORG_BUNDLE_FILENAME
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise org_mode.ConfigurationError(f"Could not read organization config at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise org_mode.ConfigurationError(f"Organization config at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise org_mode.ConfigurationError(f"Organization config at {path} is not a JSON object")

    trust = org_bundle_signing.verify_and_maybe_pin(data, org_dir())
    if not trust.ok:
        if trust.remedy == "serial":
            raise org_mode.ConfigurationError(
                f"Organization config at {path} was refused ({trust.detail}) -- refusing to start. "
                f"To deliberately go back to an older bundle, an administrator must delete "
                f"{org_bundle_signing.pinned_serial_path(org_dir())} first."
            )
        if trust.remedy == "none":
            raise org_mode.ConfigurationError(
                f"Organization config at {path} was refused ({trust.detail}) -- refusing to start."
            )
        raise org_mode.ConfigurationError(
            f"Organization config at {path} failed signing-key verification ({trust.detail}) -- "
            f"refusing to start. If a legitimate signing-key rotation is expected, an "
            f"administrator must delete {org_bundle_signing.pinned_public_key_path(org_dir())} "
            f"to re-trust a new key."
        )
    if trust.newly_pinned:
        logger.warning(
            "Organization config bundle signing key trusted for the first time (TOFU) and "
            "pinned to %s -- every future bundle must verify against this key",
            org_bundle_signing.pinned_public_key_path(org_dir()),
        )
    if org_mode.resolve_mode(data) == "org" and not trust.signed:
        raise org_mode.ConfigurationError(
            f"Organization config at {path} has \"mode\": \"org\" but is not signed -- org mode "
            "requires a signed bundle (build one with scripts/build_org_bundle.py --sign-key ...; "
            "generate a signing key first with --generate-signing-key if you haven't yet)."
        )
    org_bundle_signing.record_accepted_serial(trust, org_dir())
    return data


def log_org_config_bundle_hash(org_config: dict[str, Any]) -> None:
    """Record a hash of the installed org_bundle.json at
    every daemon startup, both to the regular log and to the audit trail,
    so tampering between one startup and the next is detectable by
    comparing hashes -- even for an install that hasn't adopted full
    signing (org_bundle_signing.py, ADR 0016) at all, and as a
    belt-and-suspenders record alongside it for one that has. Called once
    by run_app() on the same org_config load_org_config() already returned
    earlier in that function (init_privacy_filter() needs it sooner, to
    pick its fail-safe default) -- deferred until after
    init_audit_logger() so there's an audit logger to record to, and still
    not called from inside load_org_config() itself, which is also called
    from places that aren't "daemon startup" (e.g. a settings surface
    refreshing connector state) and shouldn't each add their own audit-log
    entry.
    """
    path = org_dir() / ORG_BUNDLE_FILENAME
    if not path.exists():
        logger.info("No organization config bundle installed (org_bundle.json absent)")
        return
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read organization config bundle for startup hash logging: %s", exc)
        return
    digest = org_bundle_signing.sha256_hex(raw)
    signed = bool(org_config.get(org_bundle_signing.SIGNATURE_FIELD))
    logger.info(
        "Organization config bundle at startup: sha256=%s size=%d bytes signed=%s",
        digest, len(raw), signed,
    )
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id=uuid.uuid4().hex[:12],
            connector="",
            tool="",
            tool_name="",
            summary=f"Daemon startup: organization config bundle sha256={digest} ({len(raw)} bytes, signed={signed})",
            sender="",
            decision="org_config_startup",
            auto_accept_rule="",
            latency_seconds=0.0,
            pii_detected=False,
        ))
    except Exception as exc:
        logger.warning("Audit log write failed for organization config startup hash: %s", exc)


def ignored_legacy_settings_message(names: list[str]) -> str:
    """The notice for a bundle that still carries settings the Admin page now owns."""
    return (
        "The installed bundle carries settings that are now managed on the Admin page and are ignored: "
        f"{', '.join(names)}. Change them on the Admin page; the bundle needs no edit."
    )


def load_admin_config(bundle: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """The admin config for this start, importing the bundle's settings the first time.

    Returns ``(admin, imported)``. Called once from run_app() right after the bundle is loaded;
    a malformed admin_config.json refuses to start the same way a malformed bundle does. The
    audit entry is written later by ``record_admin_config_import``, once the audit logger exists.
    """
    try:
        admin, imported = admin_config.ensure(
            bundle, mode=org_mode.resolve_mode(bundle), single_user=org_mode.is_single_user(bundle),
        )
    except admin_config.AdminConfigError as exc:
        raise org_mode.ConfigurationError(str(exc)) from exc
    legacy = admin_config.legacy_keys_in(bundle)
    if imported:
        logger.warning(
            "Imported %d settings from org_config.json into org/admin_config.json; "
            "they are now managed on the Admin page", len(legacy),
        )
    elif legacy:
        logger.warning(ignored_legacy_settings_message(legacy))
    return admin, imported


def record_admin_config_import(bundle: dict[str, Any], imported: bool) -> None:
    """Audit record that this start imported the bundle's settings into the admin config."""
    if not imported:
        return
    digest = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id=uuid.uuid4().hex[:12],
            connector="",
            tool="",
            tool_name="",
            summary=f"Daemon startup: imported organization config settings into the admin config (bundle sha256={digest})",
            sender="",
            decision="admin_config_imported",
            auto_accept_rule="",
            latency_seconds=0.0,
            pii_detected=False,
        ))
    except Exception as exc:
        logger.warning("Audit log write failed for the admin config import: %s", exc)


def _stored_connector_configs(mode: str) -> dict[str, connector_configs.LoadedConfig] | None:
    """The connector configs an organization server reads from its own data directory."""
    return connector_configs.load_all(org_dir()) if mode == "org" else None


def effective_org_config(bundle: dict[str, Any], admin: dict[str, Any]) -> dict[str, Any]:
    """The one dict every org-config reader sees: bundle, admin config, connector configs and
    built-in apps composed."""
    mode = org_mode.resolve_mode(bundle)
    return oauth_apps.effective_org_config(
        bundle, admin, mode=mode, single_user=org_mode.is_single_user(bundle),
        connectors=_stored_connector_configs(mode),
    )


def _release_org_config() -> dict[str, Any]:
    """The effective config of the server the release commands act on."""
    bundle = load_org_config()
    mode = org_mode.resolve_mode(bundle)
    single_user = org_mode.is_single_user(bundle)
    try:
        admin = admin_config.config_for(bundle, mode=mode, single_user=single_user)
    except admin_config.AdminConfigError as exc:
        raise org_mode.ConfigurationError(str(exc)) from exc
    return oauth_apps.effective_org_config(
        bundle, admin, mode=mode, single_user=single_user, connectors=_stored_connector_configs(mode),
    )


def prepare_connector_configs(bundle: dict[str, Any]) -> None:
    """The organization-mode start's connector-config step.

    A bundle on disk must be the one this server adopted when it was upgraded: an organization
    server takes no new bundle. The adopted bundle's app credentials are imported once, the
    bundle's remaining role is warned about, and every stored connector config is verified
    again, so a file that no longer verifies refuses the start."""
    path = org_dir() / ORG_BUNDLE_FILENAME
    if path.exists():
        try:
            digest = org_bundle_signing.sha256_hex(path.read_bytes())
        except OSError as exc:
            raise org_mode.ConfigurationError(f"Could not read organization config at {path}: {exc}") from exc
        if digest != org_bundle_signing.load_adopted_record(org_dir()).get("sha256"):
            raise org_mode.ConfigurationError(connector_configs.NEW_BUNDLE_REFUSAL)
        connector_configs.import_from_legacy_bundle(org_dir(), bundle, digest)
        logger.warning(connector_configs.LEGACY_BUNDLE_WARNING)
    connector_configs.load_all(org_dir())


def _audit_org_bundle_adopted() -> None:
    """Audit record that this start copied the 6.x org_config.json to org_bundle.json."""
    record = org_bundle_signing.load_adopted_record(org_dir())
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id=uuid.uuid4().hex[:12],
            connector="",
            tool="",
            tool_name="",
            summary=(
                "Daemon startup: copied org/org_config.json to org/org_bundle.json for PrivacyFence 7 "
                f"(sha256={record.get('sha256', '')}); the original is kept for a downgrade to 6.x"
            ),
            sender="",
            decision="org_bundle_adopted",
            auto_accept_rule="",
            latency_seconds=0.0,
            pii_detected=False,
        ))
    except Exception as exc:
        logger.warning("Audit log write failed for organization bundle adoption: %s", exc)


def _audit_step_up_requirement_change(change: StepUpRequirementChange) -> None:
    """The audit half of ``observe_step_up_requirement`` --
    called from ``_maybe_start_web_server()`` right after that function
    reports a change. By the time that function runs, ``run_app()`` has
    already called ``init_audit_logger()``, so there's an audit logger to
    record to -- same ordering ``log_org_config_bundle_hash`` above relies
    on. Kept out of webauthn_stepup.py itself so that module stays free of
    any audit_log.py dependency -- see its own module docstring.
    """
    decision = "step_up_requirement_enabled" if change.is_required else "step_up_requirement_disabled"
    summary = (
        "local mode's step_up.require_passkey is now enforced" if change.is_required
        else "local mode's step_up.require_passkey was turned off"
    )
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id=uuid.uuid4().hex[:12],
            connector="",
            tool="",
            tool_name="",
            summary=summary,
            sender="",
            decision=decision,
            auto_accept_rule="",
            latency_seconds=0.0,
            pii_detected=False,
        ))
    except Exception as exc:
        logger.warning("Audit log write failed for %s: %s", decision, exc)


def get_or_create_deployment_id() -> str:
    """A stable, opaque identifier for *this installation* -- not per-principal,
    and not re-generated across restarts -- stamped onto every audit entry
    (audit_log.AuditEntry.deployment_id, filled in by AuditLogger.record())
    so a centralized collection of entries -- forwarded (audit_forwarding.py)
    or manually aggregated -- can tell which employee machine or org-mode
    server produced a given decision. Persisted once at
    ``data_dir()/deployment_id`` (install-wide, like ``org_dir()``'s own
    bundle -- not per-principal like ``user_dir()``, since one daemon
    process is one deployment regardless of how many principals it serves
    in org mode) and read back on every later startup. A random hex id,
    not a hostname/MAC address -- it identifies "which install", not
    anything about the machine itself.
    """
    path = data_dir() / "deployment_id"
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError as exc:
        logger.warning("Could not read deployment id at %s: %s", path, exc)
    new_id = uuid.uuid4().hex
    try:
        atomic_write_text(str(path), new_id)
    except OSError as exc:
        logger.warning(
            "Could not persist deployment id at %s: %s -- a new one will be generated next "
            "startup", path, exc,
        )
    return new_id


def check_storage_permissions(org_mode_active: bool) -> None:
    """Startup permission check: warn (or, in org mode, refuse to start) if
    any of this install's own data directories grant group/other access --
    e.g. an install that predates ``paths.py``'s ``secure_mkdir`` adoption,
    upgraded in place, or a directory hand-created (or restored from a
    backup) under a looser umask than PrivacyFence itself would ever use.
    ``data_dir()``/``org_dir()``/``user_dir()`` already self-heal on every
    call (``secure_mkdir`` re-``chmod``s an existing directory to ``0700``
    every time it's resolved) -- this check exists for the cases that
    self-healing doesn't cover: a filesystem that silently refuses
    ``chmod`` (already logged at ``warning`` from ``secure_mkdir`` itself,
    but easy to miss in a large log), or a directory whose looser
    permissions were restored *after* the ``chmod`` that would have fixed
    them ran.

    Local mode logs every finding at ``warning`` and keeps starting -- the
    same "detectable, not necessarily preventable" posture the bundle
    hash-logging above takes. Org mode -- centrally managed, and held to a
    stricter standard -- fails closed: ``InsecurePermissionsError``
    propagates out of ``run_app()`` through the same top-level "print and
    refuse to start" path a broken org config's ``ConfigurationError``
    already uses (main()'s own ``except Exception``).
    """
    # dict.fromkeys dedupes without disturbing order -- user_dir() with no
    # principal in scope resolves to data_dir() itself (the local
    # principal's storage root *is* data_dir(), see paths.py's own
    # docstring), so at daemon startup this list often names the same
    # directory twice; no need to warn about it twice too.
    dirs = list(dict.fromkeys([data_dir(), org_dir(), user_dir()]))
    problems = audit_directory_permissions(dirs)
    for problem in problems:
        logger.warning("Insecure storage permissions: %s", problem)
    if problems and org_mode_active:
        raise InsecurePermissionsError(
            "Refusing to start in organization mode: " + " ".join(problems)
        )


def setup_logging(config: dict[str, Any]) -> None:
    log_cfg = config.get("logging", {}) or {}
    level_name = str(log_cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = _resolve_path(log_cfg.get("file", "logs/privacyfence.log"))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # Every logger in the process inherits the root logger's handlers, so this is the one
    # place that needs to redact token-shaped substrings for the whole
    # daemon rather than at each individual `except Exception` -- see
    # safe_errors.py's module docstring for what this catches and why the
    # MCP-boundary public-message allowlist (routes_mcp.py) is a separate,
    # stricter layer rather than relying on this alone.
    fmt = SecretRedactingFormatter(
        f"%(asctime)s v{__version__} %(levelname)-8s [%(name)s] %(message)s"
    )
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    for h in handlers:
        h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)

    logger.info("Logging initialized → %s", log_file)
    _flush_deferred_warnings()


# ---------------------------------------------------------------------------- #
# Web server: the organization server's /mcp, approval and admin surfaces
# share one embedded server and port. web.mcp.enabled turns /mcp on.
# ---------------------------------------------------------------------------- #

def _maybe_start_web_server(
    config: dict[str, Any],
    connector_host: ConnectorHost,
    *,
    unattended_sessions_enabled: bool,
    org_config: dict[str, Any] | None = None,
    config_path: str = "",
    bundle: dict[str, Any] | None = None,
    startup_admin: dict[str, Any] | None = None,
) -> Any:
    """Returns the started organization WebServer, or None when ``web.mcp.enabled`` is off (there
    is no other surface to fall back to: ``/mcp`` is the one transport, and the key survives as a
    deliberate full-stop kill switch). Imports the web/starlette/uvicorn/mcp stack lazily so
    constructing it is deferred to the one place that needs it.

    ``connector_host`` is already built and holds the real connector set by the time this is
    called (see run_app's ordering).

    ``org_config`` is where ``"mode"`` lives; ``resolve_mode`` refuses anything but ``org``.
    Defaults to ``{}`` when omitted, which no real caller does -- run_app() always passes the
    result of ``load_org_config()``.
    """
    web_config = config.get("web", {}) or {}
    mcp_config = web_config.get("mcp", {}) or {}
    org_config = org_config or {}
    org_mode.resolve_mode(org_config)
    mcp_enabled = bool(mcp_config.get("enabled", False))

    # ADR 0007: settings.yaml's own file_bridge: section, read once here --
    # same "resolve at startup, not on every call" posture as gate.py's
    # configure_popup_executor above. No-op (keeps local_files.py's own
    # default) when the section is absent, exactly like every other
    # settings.yaml section that ships with no config at all.
    from . import local_files
    file_bridge_config = config.get("file_bridge", {}) or {}
    local_files.configure_file_bridge(
        max_download_bytes=int(
            file_bridge_config.get("max_download_bytes", local_files.DEFAULT_MAX_DOWNLOAD_BYTES)
        ),
    )

    if not mcp_enabled:
        if os.environ.get(oidc_provider.CLIENTS_ENV, "").strip():
            logger.warning("PRIVACYFENCE_OIDC_CLIENTS is ignored: the web server is off.")
        return None
    return _start_org_web_server(
        web_config, org_config, connector_host, unattended_sessions_enabled=unattended_sessions_enabled,
        install_wide_config=config, install_wide_config_path=config_path,
        bundle=bundle, startup_admin=startup_admin,
    )


def _load_principal_settings(*, install_wide_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load ``settings.yaml`` for whichever principal is currently scoped,
    and make it *live* for that principal -- three halves of what run_app()
    does once for the local principal (auto-accept rules, and the
    privacy/PII filter too).

    Called from org mode's per-principal ``ConnectorRegistry`` factory,
    under the ``principal_scope`` that factory is already run inside. The
    *relative* path is what makes ``_resolve_authority_path()`` (and
    ``load_config``'s own bootstrap-a-default-on-first-use behavior)
    resolve against that principal's own
    ``users/<id>/authority/config/settings.yaml`` rather than the local
    principal's -- every principal's human-authority files live under its
    own ``authority`` directory (see paths.py's own docstring).

    All side effects below exist because ``ConnectorRegistry.get()``
    never goes through ``run_app()`` for any principal other than local, so
    nothing else ever performs them for an org principal:

    - ``init_config_path()`` -- without it, every non-local principal's
      ``auto_accept._REGISTRY`` entry kept its default ``config_path=None``
      forever. Invisible until something actually tried to *persist* a
      rule for that principal -- ``add_policy_v2_rules``/``remove_policy_v2_rule``
      (gate.py's "Always allow"/propose-policy-change paths) would raise
      "auto_accept config path not initialized" instead.
    - ``set_policy_v2_store_rules()`` -- without it, every org
      principal's ``_AutoAcceptState.policy_v2_store_rules`` stayed at its dataclass default
      (``[]``), so no rule -- migrated or authored directly against the v2 schema -- would ever
      auto-accept anything for that principal.
    - ``init_privacy_filter()`` -- without it, ``privacy_
      filter._REGISTRY`` (also a ``PrincipalRegistry``, see that module's
      docstring) kept its default empty-dict entry for every principal but
      whichever one happened to be scoped when a local-mode ``run_app()``
      called ``init_privacy_filter`` directly. ``category_policy()``'s own
      "group absent from the registry" fallback is a hardcoded "allow" --
      the *opposite* of org mode's intended fail-closed ``block`` default
      (``privacy_filter.init_privacy_filter``'s own ``fail_safe_default``)
      -- so every org principal but that one silently got the permissive
      default nobody configured. ``org_managed=True`` unconditionally: this
      function only ever runs from org mode's own ``ConnectorRegistry``
      factory (see this function's own docstring above), never local
      mode's. The policy is install-wide, with no per-user override (see
      docs/org-mode-setup-guide.md's "Install-wide and per-user policy")
      -- so this uses
      ``install_wide_config`` (the server's own settings.yaml, threaded
      down from ``_start_org_web_server``), not this principal's own
      per-user ``cfg``, for exactly the same reason ``cfg`` is right for
      auto-accept rules and wrong here: a per-user file nobody expects an
      admin to edit for PII policy must not silently override the one file
      they actually do edit. Falls back to ``cfg`` only when no install-wide
      config is given at all (a bare ``principal_scope()`` call in a test
      that doesn't care about privacy-filter behavior specifically).
    - ``init_pii_detection()`` -- the exact same omission as the
      one above, one module over, found while making the PII gate editable
      from org mode's admin settings page. ``pii_detector._REGISTRY`` is a
      ``PrincipalRegistry`` too, and ``run_app()``'s own call is the only
      one there has ever been, so every org principal but the launcher's
      got a default-constructed ``_PiiState`` -- detection on, both
      optional categories on -- no matter what the install's settings.yaml
      said. Unlike the privacy-filter case that default is fail-*closed*
      (it detects more, not less), so nothing was ever let through that
      shouldn't have been; what it did mean is that an admin who turned a
      category off install-wide saw it stay on for everyone -- precisely
      the disagreement a settings page that now *edits* this value cannot
      ship with. Same install-wide source, for the same reason.

    The sibling ``init_config_path`` omission was found and fixed on its own; this one
    survived it because no in-process org test had a principal whose
    settings.yaml carried a rule *and* went through the real factory.
    """
    resolved_path = _resolve_authority_path("config/settings.yaml")
    cfg = load_config(resolved_path, principal=current_principal())
    _flush_deferred_warnings()
    init_config_path(resolved_path)
    set_policy_v2_store_rules(policy_store.compile_rules_from_config(cfg))
    install_wide = install_wide_config if install_wide_config is not None else cfg
    init_privacy_filter(install_wide, org_managed=True)
    pii_config = install_wide.get("pii_detection", {}) or {}
    init_pii_detection(
        pii_config.get("enabled", True),
        detect_ip_addresses=pii_config.get("detect_ip_addresses", True),
        detect_financial_figures=pii_config.get("detect_financial_figures", True),
        audit_match_details=pii_config.get("audit_match_details", False),
    )
    return cfg


def _require_single_user_start(server_config: org_mode.ServerConfig, startup_admin: dict[str, Any] | None) -> None:
    """Refuse a single-user start that would expose a one-administrator server, or that would
    quietly turn a former organization server into one.

    The second check is what stops deleting org_bundle.json from downgrading a production
    server: it would otherwise come back as single-user mode on whatever address it was bound to.
    """
    if not org_mode.is_loopback_issuer(server_config.issuer_url):
        raise org_mode.ConfigurationError(
            "Organization mode without an identity provider is single-user mode and runs only at "
            "http://localhost or http://127.0.0.1. Set PRIVACYFENCE_IDP_ISSUER and "
            "PRIVACYFENCE_IDP_CLIENT_ID (docs/org-mode-docker.md) to serve any other address."
        )
    admin = startup_admin if startup_admin is not None else (admin_config.load() or {})
    pinned_keys = (
        org_bundle_signing.pinned_public_key_path(org_dir()),
        org_dir() / org_bundle_signing.LEGACY_PINNED_PUBKEY_FILENAME,
    )
    if (
        any(path.exists() for path in pinned_keys)
        or admin.get("org_server")
        or admin.get("imported_from_bundle_sha256")
    ):
        raise org_mode.ConfigurationError(
            "This data directory belonged to an organization server; restore org_bundle.json, or "
            "use a new data directory for single-user mode."
        )


def _start_org_web_server(
    web_config: dict[str, Any], org_config: dict[str, Any], connector_host: ConnectorHost,
    *, unattended_sessions_enabled: bool, install_wide_config: dict[str, Any],
    install_wide_config_path: str = "",
    bundle: dict[str, Any] | None = None, startup_admin: dict[str, Any] | None = None,
) -> Any:
    """org mode's own boot path -- a real OAuth 2.1 authorization server on ``/mcp``
    instead of the local shared-secret ``StaticTokenVerifier``. The
    settings surface (web/routes_settings.py's build_org_routes) is
    mounted here too now -- see web/server.py's own module docstring for
    exactly what it does and does not cover. Raises ``org_mode.
    ConfigurationError`` (surfaced as a startup failure, the same
    posture a missing/invalid settings.yaml already has) if the organization config
    is missing the ``idp``/``server`` sections org mode requires -- there is
    no silent partial-org-mode fallback.

    ``connector_host`` (the local principal's own connector set, built once
    at startup by run_app()) is accepted for signature parity with local
    mode's own call but is otherwise unused here: every org
    principal's connectors now come from ``ConnectorRegistry`` below,
    built lazily per principal instead of shared off the local principal's
    set (see connector_registry.py's own docstring).

    ``install_wide_config_path`` is where that dict came from,
    resolved -- carried on ``OrgAuth`` so the admin privacy page can write
    the policy back to the same file. Empty when a caller has no real
    settings.yaml behind the dict, which leaves that page read-only rather
    than guessing at a path to overwrite.

    ``install_wide_config`` is run_app()'s own ``config`` --
    the *server's own* settings.yaml, already loaded once at startup for
    the local/launcher principal's own ``init_privacy_filter()`` call.
    Threaded down into ``_connectors_for_principal``'s
    ``_load_principal_settings()`` call below (so every org principal's
    privacy-filter registry entry reflects the real install-wide policy,
    not their own unconfigured per-user file -- see that function's own
    docstring) and carried on ``OrgAuth.install_wide_settings`` for
    web/routes_settings.py's admin-only policy view (build_org_routes) to read directly,
    with no daemon_main import of its own needed there.
    """
    from .approvals import PendingApprovalRegistry
    from .connector_registry import ConnectorRegistry
    from .gate import configure_popup_executor
    from . import org_identity
    from .org_identity import IdpConfig
    from .web.mcp_dispatch import McpDispatcher
    from .web.oauth_provider import IDP_CALLBACK_PATH, OrgOAuthProvider
    from .web.org_session import OrgSessionStore
    from .web.server import OrgAuth, WebServer
    from .web_approval_ui import init_web_approval_ui

    server_config = org_mode.ServerConfig.from_org_config(org_config)
    # Validated here so a broken "peek"."auto_provision" refuses startup; peek_provisioning.start
    # parses it again below.
    org_mode.PeekProvisioningConfig.from_org_config(org_config)
    if org_mode._idp_present(org_config):
        # Recorded before discovery, so a server whose IdP is down at this start is still known
        # as a server at the next one.
        admin_config.mark_org_server()
        if startup_admin is not None:
            startup_admin["org_server"] = True
    idp = IdpConfig.from_org_config(org_config)
    # Validated here so a broken "grist"."auto_provision" refuses startup; the provisioner
    # itself re-reads it in grist_provisioning.start below.
    org_mode.GristProvisioningConfig.from_org_config(org_config)
    # Validated here so a malformed "agent_runtimes" section or URL refuses startup (ADR 0278-0282).
    # The key is read once without catching ConfigurationError, so a key set both inline and as
    # _FILE, or an unreadable key file, refuses startup too.
    agent_runtime = agent_runtimes.N8nRuntimeConfig.from_org_config(org_config)
    agent_runtime_key = agent_runtimes.api_key()
    agent_runtimes.check_environment(agent_runtime, agent_runtime_key)
    if agent_runtime.enabled and not (agent_runtime.base_url and agent_runtime_key):
        logger.warning(
            "agent_runtimes.n8n is enabled but its URL or API key is missing; "
            "n8n credentials are not provisioned.",
        )
    oauth_apps.check_environment_auto_provision(
        single_user=idp is None, grist_oauth=bool((org_config.get("grist") or {}).get("client_id"))
    )
    if idp is None:
        if bundle:
            # A signed organization bundle always meant a real server, and 6.x never wrote an
            # "idp" section into one; running it as single-user mode would drop the sign-in.
            raise org_mode.ConfigurationError(
                "org_bundle.json has no \"idp\" section; set PRIVACYFENCE_IDP_* and run "
                "privacyfence-app --retire-bundle."
            )
        # No identity provider: single-user mode, which is only safe where nothing but this
        # computer can reach the listener.
        _require_single_user_start(server_config, startup_admin)
    elif (data_dir() / "users" / "single-user").is_dir():
        logger.warning(
            "This data directory holds single-user data (users/single-user/); people who sign in "
            "through the identity provider cannot reach it."
        )

    approvals_config = web_config.get("approvals", {}) or {}
    approval_registry = PendingApprovalRegistry(
        hold_window=float(approvals_config.get("hold_window_seconds", 30.0)),
        pending_ttl=float(approvals_config.get("pending_ttl_seconds", 15 * 60.0)),
        ledger_ttl=float(approvals_config.get("ledger_ttl_seconds", 5 * 60.0)),
        max_pending=int(approvals_config.get("max_pending", 50)),
        # See approvals.DEFAULT_MAX_PENDING_PER_PRINCIPAL's own comment for
        # why this exists alongside max_pending above.
        max_pending_per_principal=int(approvals_config.get("max_pending_per_principal", 20)),
        # See approvals.DEFAULT_ADAPTIVE_HOLD's own comment for what this
        # collapses and why it's on by default.
        adaptive_hold=bool(approvals_config.get("adaptive_hold", True)),
    )
    # See gate.py's own _popup_executor comment: it must never hold fewer
    # workers than the registry can have approvals live, or a card past
    # that count renders as "Preparing this request" forever. This one
    # registry serves every principal in org mode, so max_pending -- the
    # whole-registry cap -- is the right number to size against, not
    # max_pending_per_principal.
    configure_popup_executor(approval_registry.max_pending)
    web_ui = init_web_approval_ui(registry=approval_registry)
    # WebApprovalUI is unconditionally the ApprovalUI here, same as local
    # mode's own _maybe_start_web_server above -- org mode never
    # had a native option to begin with (there is no GUI on a server).
    init_approval_ui(web_ui)

    def _connectors_for_principal(_principal: Principal) -> list:
        # Loaded fresh per principal, under the principal_scope
        # ConnectorRegistry.get() already enters before calling this --
        # see _load_principal_settings() for what that scope buys and why
        # both of its side effects are needed. Re-reading load_org_config()
        # here (rather than closing over the org_config this function was
        # called with) would also be defensible, but it's already loaded
        # once by run_app() and passed down consistently everywhere else in
        # this module, so this stays consistent with that. Per-principal
        # build failures aren't threaded anywhere yet -- org mode has no
        # per-principal settings page for them to surface on --
        # so they're discarded here;
        # a later phase that needs them for org mode plugs in at this seam.
        connectors, _failures = build_connectors(
            _load_principal_settings(install_wide_config=install_wide_config), org_config,
        )
        return connectors

    connector_registry = ConnectorRegistry(factory=_connectors_for_principal)
    # Does nothing unless "peek"."auto_provision" is on and the provisioning key is set. A fresh
    # token for a person drops their cached connectors so the next request sees it.
    peek_provisioning.start(org_config, on_change=connector_registry.evict, stop_event=_shutdown_event)
    grist_provisioning.start(org_config, on_change=connector_registry.evict, stop_event=_shutdown_event)
    if agent_runtime.base_url and agent_runtime_key:
        runtime_credential_sweeper.start(config=agent_runtime, stop_event=_shutdown_event)
    if idp is None and oauth_apps.environment_auto_provision():
        # Single-user install: its one person is provisioned as verified (ADR 0272).
        from .web.single_user_auth import SINGLE_USER_PRINCIPAL

        peek_provisioning.notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True)
        grist_provisioning.notify_sign_in(SINGLE_USER_PRINCIPAL, email_verified=True)

    # Layered on top of the IdP's own authentication above -- see org_identity.
    # check_authz_policy's own docstring for what this does and doesn't
    # change. Absent "authz" section in the admin config -> disabled,
    # every IdP-authenticated principal is admitted, unchanged from before.
    # Bound to the IdP so the allowed-domain check knows the issuer and the unverified-email setting.
    authz_policy = org_identity.IdpAuthzPolicy.bind(org_mode.AuthzPolicyConfig.from_org_config(org_config), idp)
    # With the built-in Google sign-in the IdP redirects to the relay, which forwards the browser
    # to /oauth/idp/callback here.
    idp_callback_url = (
        org_identity.signin_redirect_uri(
            idp, issuer_url=server_config.issuer_url, grant="idp", callback_path=IDP_CALLBACK_PATH,
        )
        if idp is not None else f"{server_config.issuer_url.rstrip('/')}{IDP_CALLBACK_PATH}"
    )
    provider = OrgOAuthProvider(
        idp, idp_callback_url=idp_callback_url, policy=authz_policy, issuer_url=server_config.issuer_url,
        # No identity provider is single-user mode: /authorize sends the browser to its own sign-in.
        single_user=idp is None,
    )
    sessions = OrgSessionStore()
    mcp_dispatcher = McpDispatcher(
        lambda: connector_registry.get(current_principal()).connectors,
        mode="org",
        unattended_sessions_enabled=unattended_sessions_enabled,
        registry=approval_registry,
        scoped_token_policy=scoped_tokens.policy_from_org_config(org_config, org_mode=True),
        agent_runtime=agent_runtime,
    )

    push_notifier, push_store = _start_web_push(org_config, approval_registry, issuer_url=server_config.issuer_url)

    _flush_deferred_warnings()

    _configure_upstream_mcp(server_config.issuer_url)

    oidc_clients = oidc_provider.clients_from_env()
    oidc = None
    if oidc_clients:
        oidc = oidc_provider.OidcProvider(
            issuer=server_config.issuer_url.rstrip("/") + "/oidc",
            clients=oidc_clients,
            key=oidc_provider.load_or_create_signing_key(org_dir() / "oidc_signing_key.pem"),
        )
        logger.info("OIDC provider for %d app(s) at %s", len(oidc_clients), oidc.issuer)

    server = WebServer(
        web_ui,
        host=server_config.bind_host, port=server_config.port,
        mcp_dispatcher=mcp_dispatcher,
        org=OrgAuth(
            provider=provider, sessions=sessions, idp=idp, issuer_url=server_config.issuer_url,
            connector_registry=connector_registry, org_config=org_config,
            install_wide_settings=install_wide_config,
            install_wide_settings_path=install_wide_config_path,
            push_notifier=push_notifier, push_store=push_store,
            bundle=bundle or {}, startup_admin=startup_admin or {}, oidc=oidc,
        ),
        ssl_certfile=server_config.cert_file or None,
        ssl_keyfile=server_config.key_file or None,
        trusted_proxies=server_config.trusted_proxies,
    )
    server.start()
    # Before the first registration: what the administrator still has to publish in DNS for the
    # built-in Google sign-in, from the last registration's answer.
    relay_client.log_missing_dns_records(org_config)
    relay_client.start_registration_loop(server_config.issuer_url, org_config, _shutdown_event)
    relay_client.request_check()
    approval_registry.set_base_url(server.base_url)
    step_up = step_up_config.StepUpConfig.from_org_config(org_config)
    if authz_policy.builtin:
        # The built-in sign-in refuses everyone it was not told to admit, restrictions or not.
        authz_summary = (
            f"built-in Google sign-in ({len(authz_policy.admin_emails)} admin(s), "
            f"{len(authz_policy.allowed_domains)} allowed domain(s), "
            f"{len(authz_policy.required_groups)} required group(s))"
        )
    elif authz_policy.enabled:
        authz_summary = (
            f"enabled ({len(authz_policy.allowed_domains)} allowed domain(s), "
            f"{len(authz_policy.required_groups)} required group(s))"
        )
    else:
        authz_summary = "disabled"
    logger.info(
        "Org mode active -- MCP-over-HTTP at %s (OAuth 2.1, DCR at %s/register), IdP %s, "
        "WebAuthn step-up %s, app-level authz policy %s, accepting Host %s",
        server.mcp_url, server.base_url, idp.issuer if idp is not None else "single-user",
        f"enabled (scope={step_up.scope})" if step_up.enabled else "disabled",
        authz_summary,
        # The Host-allowlist set, named at startup: a reverse proxy
        # forwarding a hostname this doesn't contain gets a flat 400 on
        # every request, and without this line the only way to find out
        # which names *are* accepted is to read web/server.py.
        ", ".join(sorted(server.allowed_hosts)),
    )
    return server


def _start_web_push(
    org_config: dict[str, Any], registry: PendingApprovalRegistry, *, issuer_url: str,
) -> tuple[Any, Any]:
    """Org mode's web push (ADR 0081): unless the bundle turns it off (``web_push.enabled:
    false``), load or generate the VAPID key in ``org_dir()`` beside the server's other secrets,
    and register a notifier that pushes a bare count to a principal's subscribed browsers when an
    approval is created for them. Returns ``(notifier, store)``, or ``(None, None)`` when push is
    off, which also leaves the subscription routes unmounted."""
    from . import web_push

    if not org_mode.WebPushConfig.from_org_config(org_config).enabled:
        logger.info("Web push is off for this organization (org_bundle.json web_push.enabled: false)")
        return None, None
    store = web_push.PushSubscriptionStore()
    notifier = web_push.PushNotifier(
        store=store,
        vapid_key=web_push.load_or_create_vapid_key(org_dir() / web_push.VAPID_KEY_FILE_NAME),
        subject=issuer_url, registry=registry,
        ttl_seconds=int(registry.pending_ttl),
    )
    registry.add_created_listener(notifier.on_new_approval)
    return notifier, store


def _google_client_config(org_config: dict[str, Any]) -> dict[str, Any]:
    """Wrap the bundle's flat Google app fields back into the "installed" shape
    that ``google_oauth.authorize_local`` expects."""
    google = org_config.get("google") or {}
    if not google.get("client_id") or not google.get("client_secret"):
        return {}
    return {"installed": google}


# ---------------------------------------------------------------------------- #
# Connector construction (graceful: missing org config or auth → connector skipped)
# ---------------------------------------------------------------------------- #

def _classify_connector_failure(exc: BaseException) -> str:
    """Turns a per-connector build failure into one of the three states
    status surfaces need told apart -- "never set up", "auth expired/revoked",
    or an actual runtime error -- so a later surface (the connector rows now, a
    status meta-tool later) can say which, instead of every un-built connector looking identical.

    The first two categories are matched against text this codebase's own
    call sites author themselves -- never third-party text, so safe to
    pattern-match on:

    - a bare ``FileNotFoundError`` (the real Google OAuth client's own
      behavior when a token file is absent) or "Use Authenticate…" (every
      *ClientError's/*OAuthError's own "no token found"/"is not
      authenticated" message below and in slack_client.py/
      salesforce_client.py/atlassian_oauth.py's own ``load_token_file()``
      helpers -- all phrased around the same "Use Authenticate… in
      PrivacyFence Settings" call to action) means never authenticated.
    - "organization config not installed"/"app credentials not available"
      (this function's own raises just below, for a service with no org
      bundle section or, for Telegram, no baked-in app credentials) means
      never configured.

    Anything else falls back to ``safe_errors.public_message()``, which is
    where a connector's own ``*ClientError`` (routinely wrapping a
    third-party HTTP body) gets redacted before it can reach a
    client-facing surface."""
    msg = str(exc)
    if isinstance(exc, FileNotFoundError) or "Use Authenticate…" in msg:
        return "not_authenticated"
    if "organization config not installed" in msg or "app credentials not available" in msg:
        return "no_org_config"
    return public_message(exc)


def _salesforce_report_max_pages(config: dict[str, Any]) -> int:
    """``salesforce.report_max_pages`` from settings.yaml: the most report runs one
    paged read may use. Missing means the default; anything but a whole number of
    at least 1 (a bool is not one) warns and falls back to the default."""
    value = (config.get("salesforce") or {}).get("report_max_pages", DEFAULT_REPORT_MAX_PAGES)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        logger.warning(
            "salesforce.report_max_pages must be a whole number of at least 1; using %d",
            DEFAULT_REPORT_MAX_PAGES,
        )
        return DEFAULT_REPORT_MAX_PAGES
    return value


def _configure_upstream_mcp(issuer_url: str) -> None:
    """Organization mode with ``PRIVACYFENCE_UPSTREAM_MCP_FILE`` or ``PRIVACYFENCE_UPSTREAM_MCP`` set
    (ADR 0285, 0288): load the declaration, seed its grants, register the declared tools into the policy tables, and configure the runtime. Raises
    ``org_mode.ConfigurationError`` (the start is refused) on any problem."""
    upstreams = upstream_mcp_config.load_declared()
    if upstreams is None:
        return
    upstream_mcp_registry.register(upstreams)
    try:
        seeded = admin_config.seed_upstream_grants(upstream_mcp_config.seed_payload(upstreams))
    except admin_config.AdminConfigError as error:
        raise org_mode.ConfigurationError(str(error)) from None
    if seeded:
        logger.info(
            "Seeded the domain grants of upstream(s) %s from the declaration; edit them in admin_config.json from now on.",
            ", ".join(seeded),
        )
    grants = upstream_mcp_config.GrantStore(
        admin_config.admin_config_path(), frozenset(upstream.id for upstream in upstreams),
    )
    upstream_mcp_runtime.configure(
        upstreams=upstreams, signer=upstream_claims.load_signer(issuer_url), grants=grants,
    )
    logger.info("Upstream MCP server(s): %s", ", ".join(upstream.id for upstream in upstreams))


def build_connectors(config: dict[str, Any], org_config: dict[str, Any]) -> tuple[list, dict[str, str]]:
    """Builds every enabled, currently-authenticated connector for the
    *current principal*: every credential/cache path below resolves through ``_resolve_path()``/
    ``user_dir()``, which is the local principal's own storage root unless this is called from inside a
    ``principal_scope()`` block for someone else -- see
    connector_registry.py's ``ConnectorRegistry``, which is what actually
    does that for every other principal. ``run_app()`` below still calls
    this directly, once, for the local principal only.

    Returns ``(connectors, failures)``: ``failures`` maps the name of every
    *enabled* connector that didn't get built to why, via
    ``_classify_connector_failure()`` above -- a deliberately disabled
    connector (``enabled(name)`` false) never raises, so it never gets an
    entry here; distinguishing "disabled" from "failed" is still possible,
    just from ``config`` itself rather than from this return value."""
    connectors: list[Any] = []
    failures: dict[str, str] = {}
    connectors_cfg: dict[str, dict] = config.get("connectors", {}) or {}

    def enabled(name: str) -> bool:
        return (connectors_cfg.get(name) or {}).get("enabled", True)

    google_client_config = _google_client_config(org_config)

    # Computed once, not
    # per connector -- Drive/Gmail/Confluence's own _download_file/
    # _download_attachment methods branch on connector.download_mode, and
    # (in org mode only) build a fully-qualified download_url from
    # connector.download_base_url. Local mode's own connector.download_mode
    # stays "local" and connector.download_config/download_base_url stay
    # unset, exactly the original shape.
    download_mode = org_mode.resolve_mode(org_config)
    download_config = None
    download_base_url = ""
    if download_mode == "org":
        download_config = org_mode.DownloadDeliveryConfig.from_org_config(org_config)
        # ServerConfig.from_org_config already ran (and would have raised
        # org_mode.ConfigurationError) before build_connectors was ever
        # reached in a real org-mode boot -- see _start_org_web_server,
        # which computes its own server_config first. Re-deriving it here
        # keeps build_connectors self-contained (safe to call from a test
        # or from ConnectorRegistry's own per-principal factory with no
        # other org-mode wiring already in scope) at the cost of one cheap
        # re-parse of the organization config's own "server" section.
        download_base_url = org_mode.ServerConfig.from_org_config(org_config).issuer_url.rstrip("/")

    # Gmail
    if enabled("gmail"):
        try:
            if not google_client_config:
                raise GmailClientError("Google organization config not installed")
            gmail_client = GmailClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["gmail"]),
            )
            email = gmail_client.check_connection()
            logger.info("Gmail connector ready for %s", email)
            gmail_connector = GmailConnector(gmail_client)
            gmail_connector.my_email = email
            gmail_connector.append_signature = bool(
                (config.get("gmail", {}) or {}).get("append_signature_to_drafts", False)
            )
            gmail_connector.download_config = download_config
            gmail_connector.download_base_url = download_base_url
            connectors.append(gmail_connector)
        except (GmailClientError, FileNotFoundError) as exc:
            logger.warning("Gmail connector disabled: %s", exc)
            failures["gmail"] = _classify_connector_failure(exc)

    # Drive
    if enabled("drive"):
        try:
            if not google_client_config:
                raise DriveClientError("Google organization config not installed")
            drive_client = DriveClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["drive"]),
            )
            email = drive_client.check_connection()
            logger.info("Drive connector ready for %s", email)
            drive_connector = DriveConnector(drive_client)
            drive_connector.my_email = email
            drive_connector.download_config = download_config
            drive_connector.download_base_url = download_base_url
            connectors.append(drive_connector)
        except (DriveClientError, FileNotFoundError) as exc:
            logger.warning("Drive connector disabled: %s", exc)
            failures["drive"] = _classify_connector_failure(exc)

    # Calendar
    if enabled("calendar"):
        try:
            if not google_client_config:
                raise CalendarClientError("Google organization config not installed")
            calendar_client = CalendarClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["calendar"]),
            )
            email = calendar_client.check_connection()
            logger.info("Calendar connector ready for %s", email)
            calendar_connector = CalendarConnector(calendar_client, rooms=org_config.get("rooms", []))
            calendar_connector.my_email = email
            calendar_connector.free_busy_full_details = bool(
                (config.get("calendar", {}) or {}).get("free_busy_full_event_details", True)
            )
            connectors.append(calendar_connector)
        except (CalendarClientError, FileNotFoundError) as exc:
            logger.warning("Calendar connector disabled: %s", exc)
            failures["calendar"] = _classify_connector_failure(exc)

    # Contacts
    if enabled("contacts"):
        try:
            if not google_client_config:
                raise ContactsClientError("Google organization config not installed")
            contacts_client = ContactsClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["contacts"]),
            )
            email = contacts_client.check_connection()
            logger.info("Contacts connector ready for %s", email)
            contacts_connector = ContactsConnector(contacts_client)
            contacts_connector.my_email = email
            connectors.append(contacts_connector)
        except (ContactsClientError, FileNotFoundError) as exc:
            logger.warning("Contacts connector disabled: %s", exc)
            failures["contacts"] = _classify_connector_failure(exc)

    # Tasks
    if enabled("tasks"):
        try:
            if not google_client_config:
                raise TasksClientError("Google organization config not installed")
            tasks_client = TasksClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["tasks"]),
            )
            email = tasks_client.check_connection()
            logger.info("Tasks connector ready for %s", email)
            connectors.append(TasksConnector(tasks_client))
        except (TasksClientError, FileNotFoundError) as exc:
            logger.warning("Tasks connector disabled: %s", exc)
            failures["tasks"] = _classify_connector_failure(exc)

    # Apps Script
    if enabled("apps_script"):
        try:
            if not google_client_config:
                raise AppsScriptClientError("Google organization config not installed")
            apps_script_client = AppsScriptClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["apps_script"]),
            )
            email = apps_script_client.check_connection()
            logger.info("Apps Script connector ready for %s", email)
            apps_script_connector = AppsScriptConnector(apps_script_client)
            apps_script_connector.my_email = email
            connectors.append(apps_script_connector)
        except (AppsScriptClientError, FileNotFoundError) as exc:
            logger.warning("Apps Script connector disabled: %s", exc)
            failures["apps_script"] = _classify_connector_failure(exc)

    # Slack
    if enabled("slack"):
        try:
            slack_org = org_config.get("slack") or {}
            if not slack_org.get("client_id"):
                raise SlackClientError("Slack organization config not installed")
            token = load_slack_token(_resolve_path(TOKEN_FILES["slack"]))
            slack_client = SlackClient(
                user_token=token.get("access_token", ""),
                user_cache_file=str(user_dir() / "slack_user_cache.json"),
                channel_cache_file=str(user_dir() / "slack_channel_cache.json"),
            )
            workspace = slack_client.check_connection()
            # Directory-cache warming (if stale) happens after the whole
            # connector list is built, in the background -- see
            # _warm_connector_caches() in run_app(). Doing it here,
            # synchronously, used to delay startup completing
            # until a full users.list/conversations.list re-sync finished,
            # which read as "the app isn't running yet."
            logger.info("Slack connector ready for workspace %r", workspace)
            slack_connector = SlackConnector(slack_client, limited=oauth_apps.slack_limited(slack_org))
            slack_connector.my_email = token.get("email", "")
            connectors.append(slack_connector)
        except (SlackClientError, FileNotFoundError) as exc:
            logger.warning("Slack connector disabled: %s", exc)
            failures["slack"] = _classify_connector_failure(exc)

    # Salesforce
    if enabled("salesforce"):
        try:
            sf_org = org_config.get("salesforce") or {}
            if not sf_org.get("consumer_key"):
                raise SalesforceClientError("Salesforce organization config not installed")
            token = load_salesforce_token(_resolve_path(TOKEN_FILES["salesforce"]))
            merged = {**sf_org, **token}
            salesforce_client = SalesforceClient(config=merged, token_file=_resolve_path(TOKEN_FILES["salesforce"]))
            salesforce_client.check_connection()
            salesforce_client.report_max_pages = _salesforce_report_max_pages(config)
            logger.info("Salesforce connector ready for %s", merged.get("instance_url"))
            salesforce_connector = SalesforceConnector(salesforce_client)
            salesforce_connector.download_config = download_config
            salesforce_connector.download_base_url = download_base_url
            connectors.append(salesforce_connector)
        except (SalesforceClientError, FileNotFoundError) as exc:
            logger.warning("Salesforce connector disabled: %s", exc)
            failures["salesforce"] = _classify_connector_failure(exc)

    # Jira / Confluence — share one Atlassian OAuth grant.
    atlassian_org = org_config.get("atlassian") or {}
    atlassian_token: dict[str, Any] | None = None
    if atlassian_org.get("client_id"):
        try:
            atlassian_token = load_atlassian_token(_resolve_path(TOKEN_FILES["atlassian"]))
        except AtlassianOAuthError:
            atlassian_token = None
    # Merge in client_id/client_secret so JiraClient/ConfluenceClient can
    # refresh an expired access token instead of forcing re-authentication
    # on every restart (the token file only ever holds the per-user fields).
    atlassian_config = {**atlassian_org, **(atlassian_token or {})}
    atlassian_users = AtlassianUserDirectory(
        cache_file=str(user_dir() / "atlassian_user_cache.json"),
        cloud_id=atlassian_config.get("cloud_id", ""),
        background_reports=True,
    )

    if enabled("jira"):
        try:
            if not atlassian_org.get("client_id"):
                raise JiraClientError("Atlassian organization config not installed")
            if not atlassian_token:
                raise JiraClientError("Jira is not authenticated. Use Authenticate… in PrivacyFence Settings.")
            jira_client = JiraClient(
                config=atlassian_config,
                token_file=_resolve_path(TOKEN_FILES["atlassian"]),
                user_directory=atlassian_users,
            )
            info = jira_client.check_connection()
            if jira_client.account_id:
                atlassian_config["account_id"] = jira_client.account_id
            logger.info("Jira connector ready: %s", info)
            jira_connector = JiraConnector(jira_client)
            jira_connector.my_email = atlassian_token.get("account_email", "")
            jira_connector.my_account_id = jira_client.account_id
            jira_connector.download_config = download_config
            jira_connector.download_base_url = download_base_url
            connectors.append(jira_connector)
        except (JiraClientError, FileNotFoundError) as exc:
            logger.warning("Jira connector disabled: %s", exc)
            failures["jira"] = _classify_connector_failure(exc)

    if enabled("confluence"):
        try:
            if not atlassian_org.get("client_id"):
                raise ConfluenceClientError("Atlassian organization config not installed")
            if not atlassian_token:
                raise ConfluenceClientError("Confluence is not authenticated. Use Authenticate… in PrivacyFence Settings.")
            confluence_client = ConfluenceClient(
                config=atlassian_config,
                token_file=_resolve_path(TOKEN_FILES["atlassian"]),
                user_directory=atlassian_users,
            )
            url = confluence_client.check_connection()
            logger.info("Confluence connector ready: %s", url)
            confluence_connector = ConfluenceConnector(confluence_client)
            confluence_connector.my_email = atlassian_token.get("account_email", "")
            confluence_connector.my_account_id = confluence_client.account_id
            confluence_connector.download_mode = download_mode
            confluence_connector.download_config = download_config
            confluence_connector.download_base_url = download_base_url
            connectors.append(confluence_connector)
        except (ConfluenceClientError, FileNotFoundError) as exc:
            logger.warning("Confluence connector disabled: %s", exc)
            failures["confluence"] = _classify_connector_failure(exc)

    # Jira Assets — off unless switched on (connectors.assets.enabled, else the
    # bundle's atlassian.assets), so it is guarded by assets_enabled(), not enabled().
    if assets_enabled(config, org_config):
        try:
            if not atlassian_org.get("client_id"):
                raise AssetsClientError("Atlassian organization config not installed")
            if not atlassian_token:
                raise AssetsClientError("Jira Assets is not authenticated. Use Authenticate… in PrivacyFence Settings.")
            assets_client = AssetsClient(
                config={**atlassian_config, **assets_workspace_override(config)},
                token_file=_resolve_path(TOKEN_FILES["atlassian"]),
            )
            assets_client.check_connection()
            logger.info("Jira Assets connector ready")
            assets_connector = AssetsConnector(assets_client)
            assets_connector.my_email = atlassian_token.get("account_email", "")
            assets_connector.download_config = download_config
            assets_connector.download_base_url = download_base_url
            connectors.append(assets_connector)
        except (AssetsClientError, FileNotFoundError) as exc:
            logger.warning("Jira Assets connector disabled: %s", exc)
            failures["assets"] = _classify_connector_failure(exc)

    # Telegram — the sole exception to browser OAuth (MTProto has no
    # equivalent for full user-session access). api_id/api_hash identify the
    # PrivacyFence app itself and are baked into the build (app_credentials.py),
    # not part of the organization config bundle; phone+code(+2FA) auth is
    # still per-user.
    if enabled("telegram"):
        try:
            if not org_mode.connector_enabled_by_org(org_config, "telegram"):
                raise TelegramClientError("Telegram organization config not installed")
            creds = telegram_app_credentials()
            if not creds:
                raise TelegramClientError("Telegram app credentials not available in this build")
            api_id, api_hash = creds
            session_file = _resolve_path(TOKEN_FILES["telegram"])
            if not os.path.exists(session_file) and not os.path.exists(session_file + ".session"):
                raise TelegramClientError(
                    "Telegram is not authenticated. Use Authenticate… in PrivacyFence Settings."
                )
            tg_client = TelegramPrivacyFenceClient(
                api_id=api_id,
                api_hash=api_hash,
                session_file=session_file,
                chat_cache_file=str(user_dir() / "telegram_chat_cache.json"),
            )
            # Directory-cache warming happens the same way as Slack's now --
            # see _warm_connector_caches() in run_app() -- just scheduled on
            # the web server's own event loop rather than awaited here,
            # since that's the loop every Telegram tool call now actually
            # runs on (McpDispatcher.call(), on the ASGI app's own loop) and
            # therefore the loop Telethon's client ends up bound to on its
            # first connection (see telegram_client.py).
            logger.info("Telegram connector registered (chat cache will warm in the background)")
            connectors.append(TelegramConnector(tg_client))
        except (TelegramClientError, FileNotFoundError, Exception) as exc:
            logger.warning("Telegram connector disabled: %s", exc)
            failures["telegram"] = _classify_connector_failure(exc)

    # Peek — a bearer token from Peek's device sign-in, only ever sent to the
    # server it was issued for (ADR 0152).
    if enabled("peek"):
        try:
            if not org_mode.connector_enabled_by_org(org_config, "peek"):
                raise PeekClientError("Peek organization config not installed")
            pinned = peek_bundle_base_url(org_config.get("peek") or {}, org_mode=download_mode == "org")
            record = load_peek_token(_resolve_path(TOKEN_FILES["peek"]))
            client = PeekClient(peek_resolve_server(pinned, record), record["token"])
            client.check_connection()
            logger.info("Peek connector ready for %s", client.host)
            connectors.append(PeekConnector(client, download_mode=download_mode))
        except PeekClientError as exc:
            logger.warning("Peek connector disabled: %s", exc)
            failures["peek"] = _classify_connector_failure(exc)

    # Grist
    if enabled("grist"):
        try:
            if not org_mode.connector_enabled_by_org(org_config, "grist"):
                raise GristClientError("Grist organization config not installed")
            grist_bundle = grist_bundle_settings(org_config.get("grist") or {}, org_mode=download_mode == "org")
            token_path = _resolve_path(TOKEN_FILES["grist"])
            server_url, credential = grist_resolve_credential(grist_bundle, load_grist_token(token_path), token_path)
            grist_client = GristClient(server_url, credential)
            grist_client.check_connection()
            grist_connector = GristConnector(grist_client)
            grist_connector.download_mode = download_mode
            grist_connector.download_config = download_config
            grist_connector.download_base_url = download_base_url
            connectors.append(grist_connector)
        except GristClientError as exc:
            logger.warning("Grist connector disabled: %s", exc)
            failures["grist"] = _classify_connector_failure(exc)

    # Miro
    if enabled("miro"):
        try:
            if not org_mode.connector_enabled_by_org(org_config, "miro"):
                raise MiroClientError("Miro organization config not installed")
            miro_config = miro_bundle_settings(org_config.get("miro") or {})
            token_path = _resolve_path(TOKEN_FILES["miro"])
            provider = MiroTokenProvider(miro_config, token_path, load_miro_token(token_path))
            miro_client = MiroClient(provider)
            miro_client.check_connection()
            connectors.append(MiroConnector(miro_client))
        except MiroClientError as exc:
            logger.warning("Miro connector disabled: %s", exc)
            failures["miro"] = _classify_connector_failure(exc)

    if upstream_mcp_runtime.configured():
        connectors.extend(upstream_mcp_runtime.connectors_for_principal())

    return connectors, failures


def _warm_connector_caches(connectors: list, web_loop: asyncio.AbstractEventLoop) -> None:
    """Kick off each connector's directory-cache freshness check (Slack's
    user/channel snapshots, Telegram's chat snapshot) in the background,
    right after the web server's event loop is known (see run_app()). Both
    ensure_directories_fresh() and ensure_chat_directory_fresh() are
    best-effort and never raise -- a failure here just means the cache
    stays stale until the next lazy lookup or the explicit
    slack_refresh_*/telegram_refresh_chat_cache tool, same as if this
    warming never ran.

    Deliberately not run inline in build_connectors(), and not awaited
    here either: a full weekly re-sync (users.list/conversations.list/
    get_dialogs) can take a while on a large workspace/account, and running
    it synchronously on the main thread used to delay startup completing
    until it finished -- which read as "the app isn't running
    yet."

    Slack's client is synchronous (blocking HTTP via slack_sdk), so it gets
    its own plain background thread. Telegram's is asyncio-native
    (Telethon) and its client binds to whichever event loop first connects
    it -- that has to be ``web_loop`` (the ASGI app's own loop, captured by
    web/server.py's WebServer -- see run_app()'s wait_until_ready() call),
    the same loop every Telegram tool call now actually runs on
    (McpDispatcher.call()), not a throwaway loop of some other thread -- so
    it's scheduled there via run_coroutine_threadsafe instead.
    """
    for connector in connectors:
        if isinstance(connector, SlackConnector):
            threading.Thread(
                target=connector.client.ensure_directories_fresh,
                name="slack-cache-warm",
                daemon=True,
            ).start()
        elif isinstance(connector, TelegramConnector):
            future = asyncio.run_coroutine_threadsafe(
                connector.client.ensure_chat_directory_fresh(), web_loop
            )
            future.add_done_callback(_log_cache_warm_failure)


def _log_cache_warm_failure(future: "concurrent.futures.Future[None]") -> None:
    # Defensive only -- ensure_chat_directory_fresh() is documented never to
    # raise, same as ensure_directories_fresh(). If it somehow does, this is
    # a background warm with nothing waiting on its result, so log instead
    # of letting the exception vanish into the event loop's default handler.
    exc = future.exception()
    if exc is not None:
        logger.warning("Background Telegram cache warm failed: %s", exc)


# ---------------------------------------------------------------------------- #
# Main app
# ---------------------------------------------------------------------------- #

def run_app(config: dict[str, Any], config_path: str) -> int:
    if not _acquire_instance_lock():
        # Finding one already running is an expected outcome, not a failure
        # -- a second start of the service, or a human who ran the CLI or
        # double-clicked the exe a second time -- so INFO and exit 0, not an
        # ERROR line. The stderr message stays, since that human still wants
        # to know why nothing happened.
        logger.info("Another instance is already running; exiting.")
        print("PrivacyFence daemon is already running.", file=sys.stderr)
        return 0

    init_config_path(_resolve_path(config_path))

    set_policy_v2_store_rules(policy_store.compile_rules_from_config(config))
    # The settings.yaml-configurable rule_suggestion_priority is retired
    # (every matching auto-accept rule now gets its own "Always allow" button, so
    # there's nothing left to prioritize or exclude) and this function logged an
    # explicit "ignoring this key" notice for anyone with a pre-existing config
    # block for it. That notice has served its purpose and is gone -- a leftover
    # rule_suggestion_priority block in an old settings.yaml now falls through to
    # the same silent "unknown key is inert" handling as any other retired
    # settings.yaml key, per auto_accept.py's SUGGESTION_FAMILIES comment.
    pii_config = config.get("pii_detection", {}) or {}
    init_pii_detection(
        pii_config.get("enabled", True),
        detect_ip_addresses=pii_config.get("detect_ip_addresses", True),
        detect_financial_figures=pii_config.get("detect_financial_figures", True),
        audit_match_details=pii_config.get("audit_match_details", False),
    )
    # Loaded here, ahead of its previous spot just before build_connectors(),
    # so init_privacy_filter knows whether this install is org-
    # managed before it picks a fail-safe default for a genuinely absent
    # privacy group -- still the one load_org_config() call for this whole
    # function, its ConfigurationError still surfacing through the
    # same top-level "print and refuse to start" path in main().
    bundle = load_org_config()
    # ADR 0089: a 5.0.0a1 bundle that still names the removed mcp.tool_annotations raises
    # ConfigurationError -- refuse to start rather than quietly advertise something other than
    # what the administrator configured.
    org_mode.reject_removed_tool_annotations(bundle)
    if org_mode.resolve_mode(bundle) == "org":
        prepare_connector_configs(bundle)
    admin, admin_imported = load_admin_config(bundle)
    # Before the web server is built (it loads the scoped token store, whose tokens must already
    # hold their own rules) and for every person, signed in or not.
    data_format.migrate_everything(log=logger.warning)
    # Same "org mode fails closed, local mode warns" posture as the
    # rest of this function's fail-safe defaults now that mode is known.
    check_storage_permissions(org_mode.resolve_mode(bundle) == "org")
    init_privacy_filter(config, org_managed=org_mode.resolve_mode(bundle) == "org")
    # From here on every reader gets the effective config; the raw bundle stays for the
    # signature, the hash and the mode checks above.
    org_config = effective_org_config(bundle, admin)
    if org_mode.is_single_user(org_config):
        logger.warning("Single-user mode: one administrator, no identity provider, on this computer only.")
    for warning in check_consistency_warnings():
        logger.warning(warning)

    # Centralized audit forwarding is org-mode-only (see
    # org_mode.AuditForwardingConfig's own docstring) -- a local-mode
    # install's organization config could theoretically carry an
    # "audit_forwarding" section, but there is no "centralize" to speak of
    # for a single employee's own machine, so it's ignored outside org
    # mode regardless of what the section says.
    audit_forwarder = None
    audit_forwarding_config = org_mode.AuditForwardingConfig.from_org_config(org_config)
    if audit_forwarding_config.enabled and org_mode.resolve_mode(org_config) == "org":
        try:
            sender = audit_forwarding.build_sender(audit_forwarding_config)
            audit_forwarder = audit_forwarding.AuditForwarder(sender)
            logger.info("Audit-log forwarding enabled (%s)", audit_forwarding_config.kind)
        except Exception as exc:
            logger.warning("Could not start audit-log forwarding -- continuing without it: %s", exc)

    audit_logger = init_audit_logger(
        # The audit log (plus its HMAC key) is one of the human-authority
        # files -- authority_root(), not data_dir() itself.
        str(authority_root(Path(data_dir())) / "logs" / "audit"),
        deployment_id=get_or_create_deployment_id(),
        security_config_hash=compute_security_config_hash(config),
        forwarder=audit_forwarder,
    )
    audit_logger.export_all_pending()

    # log_org_config_bundle_hash() needs the audit logger initialized above
    # (it records to it, see its own docstring) -- org_config itself was
    # already loaded earlier, ahead of init_privacy_filter(), so its
    # org_managed fail-safe default is known before that call.
    log_org_config_bundle_hash(bundle)
    record_admin_config_import(bundle, admin_imported)
    if _bundle_adopted_this_start:
        _audit_org_bundle_adopted()
    connectors, connector_failures = build_connectors(config, org_config)
    if not connectors:
        logger.warning("No connectors could be initialized; daemon still starting.")

    unattended_enabled = bool((org_config.get("unattended_sessions", {}) or {}).get("enabled", False))
    connector_host = ConnectorHost(connectors)

    # Built after connector_host so the MCP dispatcher (if web.mcp.enabled)
    # can poll connector_host.connectors for the live connector set -- see
    # _maybe_start_web_server's own docstring.
    server = _maybe_start_web_server(
        config, connector_host, unattended_sessions_enabled=unattended_enabled,
        org_config=org_config, bundle=bundle, startup_admin=admin,
        # Resolved, not the raw --config argument -- org mode's
        # admin privacy page writes this file back, and it must land on the
        # same path run_app() read `config` from.         config_path=_resolve_path(config_path),
    )

    # Every connector call now runs on the embedded web server's own ASGI
    # event loop (McpDispatcher.call(), via /mcp) -- there is no separate
    # IPC loop to wait on any more, so Telegram's cache warm (the one piece
    # that needs a live loop, not just a thread -- see
    # _warm_connector_caches' own docstring) waits for that loop to be
    # captured instead. None when this daemon never starts the web server
    # at all -- that's only org mode with mcp.enabled off (see
    # _maybe_start_web_server's own docstring); local mode always starts
    # one now, so there's always a loop to wait for there.
    web_loop = server.wait_until_ready(timeout=5) if server is not None else None
    logger.info("Startup complete")

    if web_loop is not None:
        _warm_connector_caches(connectors, web_loop)
    elif server is not None:
        logger.warning("Web server event loop not ready in time; skipping background cache warm")

    threading.Thread(
        target=_run_personal_data_sweep, name="atlassian-personal-data-sweep", daemon=True,
    ).start()

    try:
        _wait_for_shutdown()
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
    finally:
        audit_logger.close()
        _release_instance_lock()
    return 0


# ---------------------------------------------------------------------------- #
# --revoke-scoped-tokens
# ---------------------------------------------------------------------------- #

def run_revoke_scoped_tokens(principal_id: str) -> int:
    """The administrator's way to take back every scoped token one person
    holds (ADR 0165), for when their access is revoked for good. Runs as the
    account that owns the data (``main`` dispatches it after the runtime
    identity check) and writes the person's ``scoped_tokens.json`` directly;
    a running daemon notices the change the next time a token is presented.

    ``principal_id`` must be ``local`` or name an existing ``users/<id>``
    directory: ``paths.authority_dir()`` creates the folder it is asked for,
    so an unknown account is refused before it is ever called.
    """
    from . import scoped_tokens
    from .web.mcp_auth import scoped_token_principals

    known = (
        safe_principal_id(principal_id) == principal_id
        and (principal_id == LOCAL_PRINCIPAL_ID or (data_dir() / "users" / principal_id).is_dir())
    )
    if not known:
        print("No such account.", file=sys.stderr)
        return 1
    store = scoped_tokens.ScopedTokenStore(scoped_token_principals)
    revoked = store.revoke_all(principal_id)
    print(f"Revoked {revoked} scoped token(s) for {principal_id}.")
    print("A running PrivacyFence server deletes these tokens' n8n credentials within 5 minutes.")
    return 0


# ---------------------------------------------------------------------------- #
# --relay-dns-records
# ---------------------------------------------------------------------------- #

def _bundle_section(bundle: dict[str, Any], key: str) -> dict[str, Any]:
    value = bundle.get(key)
    return value if isinstance(value, dict) else {}


def _bundle_for_migration() -> dict[str, Any] | None:
    """The adopted legacy bundle, verified, for the two commands that move its settings to the
    environment; None (after saying why) when this server has no organization bundle."""
    bundle = load_org_config()
    if not (org_dir() / ORG_BUNDLE_FILENAME).exists():
        print("There is no org/org_bundle.json on this server.", file=sys.stderr)
        return None
    if bundle.get("mode") != "org":
        print("org/org_bundle.json is not an organization bundle.", file=sys.stderr)
        return None
    return bundle


def run_print_env_from_bundle(secret_file: str | None) -> int:
    """Print the ``PRIVACYFENCE_*`` lines that say what the legacy bundle's ``mode``, ``server`` and
    ``idp`` say. The identity provider's client secret is never printed: it goes to
    ``secret_file`` (new, mode 0600) or is a placeholder."""
    try:
        bundle = _bundle_for_migration()
    except org_mode.ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    if bundle is None:
        return 1
    secret_path = Path(os.path.abspath(Path(secret_file).expanduser())) if secret_file else None
    if secret_path is not None and secret_path.exists():
        print(f"{secret_path} already exists; it is not overwritten.", file=sys.stderr)
        return 1

    def listed(value: Any) -> str:
        return ",".join(str(item) for item in value) if isinstance(value, list) else str(value)

    lines = [f"{org_mode.MODE_ENV}=org"]
    server = _bundle_section(bundle, "server")
    for key, name in (
        ("bind_host", org_mode.BIND_HOST_ENV), ("port", org_mode.PORT_ENV),
        ("issuer_url", org_mode.ISSUER_URL_ENV), ("trusted_proxies", org_mode.TRUSTED_PROXIES_ENV),
    ):
        if server.get(key) not in (None, "", []):
            lines.append(f"{name}={listed(server[key])}")
    if server.get("tls"):
        lines.append("# server.tls has no variable: end TLS at the reverse proxy (see the Docker guide).")
    idp = _bundle_section(bundle, "idp")
    for key, name in (
        ("issuer", org_mode.IDP_ISSUER_ENV), ("client_id", org_mode.IDP_CLIENT_ID_ENV),
        ("admin_group_claim", org_mode.IDP_ADMIN_GROUP_CLAIM_ENV),
        ("admin_group_values", org_mode.IDP_ADMIN_GROUP_VALUES_ENV),
        ("step_up_acr_values", org_mode.IDP_STEP_UP_ACR_VALUES_ENV),
        ("admin_emails", org_mode.ADMIN_EMAILS_ENV),
    ):
        if idp.get(key) not in (None, "", []):
            lines.append(f"{name}={listed(idp[key])}")
    if "trust_unverified_email" in idp:
        lines.append(f"{org_mode.IDP_TRUST_UNVERIFIED_EMAIL_ENV}={'true' if idp['trust_unverified_email'] else 'false'}")
    secret = idp.get("client_secret")
    if secret:
        if secret_path is None:
            lines.append(f"{org_mode.IDP_CLIENT_SECRET_ENV}=<the identity provider's client secret>")
        else:
            try:
                fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"{secret}\n")
            except OSError as exc:
                print(f"Could not write {secret_path}: {exc.strerror}", file=sys.stderr)
                return 1
            lines.append(f"{org_mode.IDP_CLIENT_SECRET_FILE_ENV}={secret_path}")
    for line in lines:
        print(line)
    return 0


def _first_bundle_difference(bundle: dict[str, Any]) -> str | None:
    """What the environment says differently from the legacy bundle for the server's address
    and the identity provider's issuer and client id, or None when it says the same."""
    server = _bundle_section(bundle, "server")
    bundle_address = str(server.get("issuer_url") or "").strip()
    env_address = os.environ.get(org_mode.ISSUER_URL_ENV, "").strip()
    if bundle_address and env_address != bundle_address:
        return f"{org_mode.ISSUER_URL_ENV} is not the server address org_bundle.json has ({bundle_address})."
    bundle_idp = _bundle_section(bundle, "idp")
    env_idp = org_mode.idp_section({}) or {}
    for key, name in (("issuer", org_mode.IDP_ISSUER_ENV), ("client_id", org_mode.IDP_CLIENT_ID_ENV)):
        if str(env_idp.get(key) or "") != str(bundle_idp.get(key) or ""):
            return f"{name} is not the identity provider {key.replace('_', ' ')} org_bundle.json has ({bundle_idp.get(key)})."
    return None


def run_retire_bundle() -> int:
    """End the legacy bundle once the environment says what it said: import its app credentials
    (a server that was never started under 7 still has them only there), record ``retired`` and
    delete ``org/org_bundle.json``."""
    try:
        bundle = _bundle_for_migration()
        if bundle is None:
            return 1
        difference = _first_bundle_difference(bundle)
    except org_mode.ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    if difference is not None:
        print(f"Not retiring org_bundle.json: {difference}", file=sys.stderr)
        return 1
    path = org_dir() / ORG_BUNDLE_FILENAME
    record = org_bundle_signing.load_adopted_record(org_dir())
    try:
        digest = org_bundle_signing.sha256_hex(path.read_bytes())
        if digest != record.get("sha256"):
            raise org_mode.ConfigurationError(connector_configs.NEW_BUNDLE_REFUSAL)
        connector_configs.import_from_legacy_bundle(org_dir(), bundle, digest)
        record = org_bundle_signing.load_adopted_record(org_dir())
        record["retired"] = True
        atomic_write_json(org_bundle_signing.adopted_record_path(org_dir()), record, indent=2)
        path.unlink()
    except (OSError, org_mode.ConfigurationError) as exc:
        print(f"Could not retire org_bundle.json: {exc}", file=sys.stderr)
        return 1
    print("org_bundle.json is retired. Its app credentials are connector configs; the environment now holds the rest.")
    return 0


def run_relay_dns_records() -> int:
    """Print the lines the server logs for each sign-in domain the relay has not found proven in
    DNS, so an administrator can publish the records before anyone can sign in. Reads the last
    registration's answer from the data directory; a running server checks again within the hour,
    and at its next start."""
    try:
        org_config = _release_org_config()
    except org_mode.ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    if relay_client.signin_domains(org_config) is None:
        print("This server does not use the built-in Google sign-in; it needs no DNS records.")
        return 0
    lines = relay_client.missing_dns_records(org_config)
    for line in lines:
        print(line)
    if not lines:
        print("Built-in Google sign-in: every sign-in domain is proven in DNS.")
    return 0


# ---------------------------------------------------------------------------- #
# --peek-release
# ---------------------------------------------------------------------------- #

def run_peek_release(principal_id: str) -> int:
    """The administrator's way to take back the Peek token PrivacyFence minted
    for one person and stop renewing it, for when they have left. Runs as the
    account that owns the data (``main`` dispatches it after the runtime
    identity check) in its own process, so it neither reaches a running
    daemon's connector registry nor needs one: a daemon keeps its cached
    connector, and so the revoked token, until it is evicted, and Peek
    refuses the token meanwhile. The Peek account stays. A person who
    signs in again is provisioned again, so remove them from the identity
    provider first.
    """
    known = (
        safe_principal_id(principal_id) == principal_id
        and principal_id != LOCAL_PRINCIPAL_ID
        and (data_dir() / "users" / principal_id).is_dir()
    )
    if not known:
        print("No such account.", file=sys.stderr)
        return 1
    try:
        config = org_mode.PeekProvisioningConfig.from_org_config(_release_org_config())
    except org_mode.ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not config.enabled:
        print("Peek auto-provisioning is not enabled in the organization config.", file=sys.stderr)
        return 1
    try:
        key = org_mode.provisioning_key(config.key_env)
    except org_mode.ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not key:
        print(f"Set {config.key_env} to the Peek provisioning key first.", file=sys.stderr)
        return 1
    # Local state first: it stops a running daemon's worker from re-linking and
    # minting between the two steps. A failed revoke is repeated by running the
    # command again.
    peek_provisioning.release(principal_id)
    try:
        had_link = PeekProvisioningClient(config.base_url, key).release_account(principal_id)
    except PeekClientError as exc:
        print(exc, file=sys.stderr)
        return 1
    message = f"Released the Peek connection of {principal_id}. The Peek account stays."
    message += (
        " PrivacyFence's token for it is revoked." if had_link else " Peek had no provisioned token for it."
    )
    print(message)
    return 0


# ---------------------------------------------------------------------------- #
# --grist-release
# ---------------------------------------------------------------------------- #

def run_grist_release(principal_id: str) -> int:
    """The administrator's way to give back the Grist key PrivacyFence holds for
    one person who has left. PrivacyFence forgets the key first, then asks the
    Grist provisioner to delete it in Grist when the provisioner created it.
    Run it as the account that owns the data, with the provisioning key in the
    environment. A person who signs in again is provisioned again.
    """
    from .grist_provisioner_api import GristProvisionerClient, GristProvisionerClientError

    known = (
        safe_principal_id(principal_id) == principal_id
        and principal_id != LOCAL_PRINCIPAL_ID
        and (data_dir() / "users" / principal_id).is_dir()
    )
    if not known:
        print("No such account.", file=sys.stderr)
        return 1
    try:
        config = org_mode.GristProvisioningConfig.from_org_config(_release_org_config())
    except org_mode.ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not config.enabled:
        print("Grist auto-provisioning is not enabled in the organization config.", file=sys.stderr)
        return 1
    try:
        key = org_mode.provisioning_key(config.key_env)
    except org_mode.ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not key:
        print(f"Set {config.key_env} to the Grist provisioning key first.", file=sys.stderr)
        return 1
    # Before the provisioner call, so a worker already holding this person stops.
    grist_provisioning.release(principal_id)
    try:
        result = GristProvisionerClient(config.provisioner_url, key).release_account(principal_id)
    except GristProvisionerClientError as exc:
        print(
            f"{exc} PrivacyFence has already forgotten the key; run --grist-release again to delete it in Grist.",
            file=sys.stderr,
        )
        return 1
    message = f"Released the Grist connection of {principal_id}."
    if result.revoked:
        message += " The Grist API key PrivacyFence was given is deleted in Grist."
    elif result.released:
        message += (
            " Its Grist API key was the person's own, or was already replaced, and stays; "
            "delete it in Grist or disable the account there."
        )
    else:
        message += " The Grist provisioner had no link for it."
    print(message)
    return 0


# ---------------------------------------------------------------------------- #
# Argument parsing
# ---------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="privacyfence-app",
        description="PrivacyFence daemon — governance UI and connector host.",
    )
    # authority_root(), not PROJECT_ROOT directly -- settings.yaml
    # is the human's privacy policy, not the agent's own operational data.
    default_config = str(authority_root(Path(PROJECT_ROOT)) / "config" / "settings.yaml")
    parser.add_argument("--config", default=default_config)
    # ADR 0165: an administrator takes back all of one person's scoped tokens.
    parser.add_argument(
        "--revoke-scoped-tokens", metavar="PRINCIPAL_ID", default=None,
        help=(
            "Revoke every scoped token the account PRINCIPAL_ID holds ('local' in local mode, or the "
            "principal id the audit log and the org Settings page show) and exit. Run it as the "
            "account the daemon runs as."
        ),
    )
    parser.add_argument(
        "--relay-dns-records", action="store_true",
        help=(
            "Print the DNS TXT records the built-in Google sign-in still needs, one line per sign-in "
            "domain the relay has not found yet, and exit."
        ),
    )
    parser.add_argument(
        "--print-env-from-bundle", action="store_true",
        help=(
            "Print the PRIVACYFENCE_* lines that say what the legacy org/org_bundle.json says about the "
            "mode, the server and the identity provider, and exit. The identity provider's client secret "
            "is never printed."
        ),
    )
    parser.add_argument(
        "--idp-secret-file", metavar="PATH", default=None,
        help=(
            "With --print-env-from-bundle: write the identity provider's client secret to PATH (a new "
            "file, mode 0600) and print the variable that names it."
        ),
    )
    parser.add_argument(
        "--retire-bundle", action="store_true",
        help=(
            "Retire the legacy org/org_bundle.json once the environment says the same mode, server address "
            "and identity provider: its app credentials become connector configs and the file is deleted."
        ),
    )
    parser.add_argument(
        "--peek-release", metavar="PRINCIPAL_ID", default=None,
        help=(
            "Revoke the Peek token PrivacyFence minted for the account PRINCIPAL_ID, stop renewing it, "
            "and exit. The Peek account and its pages stay. Needs the provisioning key in the environment."
        ),
    )
    parser.add_argument(
        "--grist-release", metavar="PRINCIPAL_ID", default=None,
        help=(
            "Release the Grist key PrivacyFence holds for the account PRINCIPAL_ID: the provisioner deletes "
            "it in Grist if it created it, PrivacyFence forgets it, and the daemon exits. Needs the "
            "provisioning key in the environment."
        ),
    )
    args = parser.parse_args(argv)
    if args.idp_secret_file and not args.print_env_from_bundle:
        parser.error("--idp-secret-file goes with --print-env-from-bundle")
    return args


# Why main() last returned 1, for a caller that cannot read stderr.
_last_startup_error: str | None = None


def last_startup_error() -> str | None:
    return _last_startup_error


def _note_startup_error(message: str) -> None:
    global _last_startup_error
    _last_startup_error = message


def _refuse_to_start(message: str, *, logged: bool = False) -> int:
    """Record ``message`` as the reason main() is returning 1, and print it to stderr.

    ``logged`` once setup_logging() has run, to put it in the log file too -- the same pair the
    "Fatal error" path writes. Before that there is no log file to write to (settings.yaml names
    it).
    """
    _note_startup_error(message)
    if logged:
        logger.error("%s", message)
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Before anything reads or rewrites data: a newer release's data is not ours to touch.
    try:
        data_format.check_upgrade_source(paths.data_dir())
    except org_mode.ConfigurationError as exc:
        return _refuse_to_start(f"Configuration error: {exc}")

    if args.revoke_scoped_tokens is not None:
        return run_revoke_scoped_tokens(args.revoke_scoped_tokens)
    if args.relay_dns_records:
        return run_relay_dns_records()
    if args.print_env_from_bundle:
        return run_print_env_from_bundle(args.idp_secret_file)
    if args.retire_bundle:
        return run_retire_bundle()
    if args.peek_release is not None:
        return run_peek_release(args.peek_release)
    if args.grist_release is not None:
        return run_grist_release(args.grist_release)

    try:
        config = load_config(args.config, principal=LOCAL_PRINCIPAL)
    except (FileNotFoundError, ValueError) as exc:
        return _refuse_to_start(f"Configuration error: {exc}")

    setup_logging(config)

    try:
        return run_app(config, args.config)
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        print(f"Fatal error: {exc}", file=sys.stderr)
        _note_startup_error(f"Fatal error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
