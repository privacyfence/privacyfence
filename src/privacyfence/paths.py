"""Centralized path resolution for PrivacyFence.

In a source checkout (editable dev install, or no install at all): data
lives in the project root. In a bundled .app, or a real (non-editable)
``pip``/``pipx install privacyfence``: data lives under a per-user data
directory so it survives app updates/reinstalls -- see is_bundled() and
_is_installed_package(). On POSIX that's ``~/.privacyfence``; on Windows
it's ``%LOCALAPPDATA%\\PrivacyFence`` -- see windows_data_dir() for why
that's a different convention rather than the same dotfile name reused
under ``%USERPROFILE%``.

#428 Phase 4 adds a third answer on top of those two: an install that has
opted into privilege separation (all three desktop platforms) keeps
everything under a system root owned by a dedicated service account instead
-- ``%ProgramData%\\PrivacyFence`` on Windows, where a service account cannot
sensibly own something inside a user profile -- with a small
``handoff_dir()`` the logged-in user's own session can still reach. See
privilege_separation.py for the layout and for what that boundary does and
does not claim.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import privilege_separation
from .secure_files import secure_mkdir

if TYPE_CHECKING:
    from .principal import Principal

logger = logging.getLogger(__name__)

# Deliberately strict -- principal ids reach here from an OAuth 2.1/OIDC
# `sub` claim once P7 lands (today it's always "local"), and this is the one
# place that string becomes a filesystem path component. Anything outside
# this set (a "/", a leading "." that could hide a directory, ...) is
# rejected rather than sanitized, so a hostile or malformed id fails loudly
# instead of silently resolving somewhere unintended. The character class
# alone would still accept "." and ".." (both are made entirely of allowed
# characters) -- _is_safe_principal_id() below rejects those two literally,
# since they're path-traversal components in their own right, not just via
# an excluded character.
_SAFE_PRINCIPAL_ID = re.compile(r"^[A-Za-z0-9._@-]{1,200}$")


def _is_safe_principal_id(principal_id: str) -> bool:
    return principal_id not in (".", "..") and bool(_SAFE_PRINCIPAL_ID.match(principal_id))


def safe_principal_id(raw: str) -> str:
    """``raw`` unchanged if it's already filesystem-safe, otherwise a
    stable hash of it (P7: an OIDC ``sub`` claim is opaque per spec and may
    contain characters ``_is_safe_principal_id`` rejects -- hashing keeps
    org_identity.py's ``principal_from_claims`` always able to produce a
    ``Principal`` rather than letting a login fail on an oddly-formatted
    but legitimate subject). Deterministic, so the same IdP subject always
    maps to the same storage directory across logins."""
    if _is_safe_principal_id(raw):
        return raw
    return "idp-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_bundled() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def is_windows() -> bool:
    """Indirection around ``os.name == "nt"`` purely so tests can monkeypatch
    this one function instead of ``os.name`` itself -- ``os.name`` is a
    real global the test process's own machinery (pytest's Path-based
    reporting included) keeps relying on for the rest of that same process,
    so flipping it for the duration of a test is unsafe even when the test
    restores it afterwards. Not underscore-prefixed: web/session_auth.py's
    ``unauthorized_html`` also branches on it, to show a Windows reader the
    right discovery-file path and shell command rather than a POSIX one."""
    return os.name == "nt"


def _is_installed_package() -> bool:
    """True for a normal (non-editable) ``pip``/``pipx install privacyfence``
    -- i.e. this file living under some ``site-packages``/``dist-packages``
    -- as opposed to a source checkout, editable dev install included: an
    editable install (``pip install -e .``, what every documented dev/source
    setup in this repo uses) keeps the real ``.py`` files at their original
    checkout location, so ``__file__`` still resolves under the repo root
    exactly as it does with no install step run at all. Checking the path
    rather than "is this package installed" is what makes that distinction
    -- ``importlib.metadata`` reports a distribution as installed either
    way. Without this, ``data_dir()`` would fall to its ``else`` branch for
    a real PyPI install too, landing config/credentials/logs somewhere
    inside site-packages instead of a real per-user data directory."""
    return "site-packages" in Path(__file__).resolve().parts or "dist-packages" in Path(__file__).resolve().parts


def windows_data_dir() -> Path:
    """``%LOCALAPPDATA%\\PrivacyFence`` -- the per-machine "Known Folder"
    Windows apps use for their own app data, as opposed to reusing
    ``~/.privacyfence`` (a dotfile under ``%USERPROFILE%``) unchanged.

    A dot-prefixed name is not a hiding convention on Windows the way it is
    on POSIX -- Explorer doesn't treat it specially, so it would just show
    up as an ordinary, oddly-named folder sitting directly in the user's
    profile root. ``%LOCALAPPDATA%`` (itself hidden by default, unlike
    ``%USERPROFILE%``) is the idiomatic location, and specifically the
    *Local* rather than *Roaming* (``%APPDATA%``) one: this directory holds
    credentials and audit logs alongside config, and those shouldn't follow
    a roaming profile across machines the way small settings might.

    Falls back to ``~\\AppData\\Local`` if ``LOCALAPPDATA`` isn't set (a
    stripped-down environment invoking the process without a full user
    profile) -- ``Path.home()`` resolves unconditionally, unlike the env
    var.
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return base / "PrivacyFence"


def data_dir() -> Path:
    """Root directory for org-wide/install-wide data (org config, the local
    web/MCP tokens, the instance lock) -- see user_dir() for a specific
    principal's own storage root, which is what most callers actually want
    for anything that's per-user data.

    Created (or re-tightened, on an upgrade from a pre-SEC-09 install) to
    ``0700`` via ``secure_mkdir`` -- see that function's own docstring and
    ``docs/security-and-compliance.md``'s "Storage format and permissions"
    section for what this closes. (``secure_mkdir``'s ``chmod`` is a no-op
    best-effort on Windows, which has no POSIX permission bits to set --
    see that function's own docstring.)

    #428 Phase 4: on an install that has opted into privilege separation
    (``scripts/{macos,linux}_privilege_separation.sh``,
    ``scripts/windows_privilege_separation.ps1``), every branch below is
    bypassed for the service-owned system root instead. A
    service account cannot sensibly own a directory inside a human's home,
    so the whole data directory moves rather than just the authority subtree
    -- which is also what makes the migration carry live connector OAuth
    tokens, and why it ships opt-in for a release before defaulting on. The
    root is ``0711`` there, not ``0700``: the logged-in user has to be able
    to traverse it to reach ``handoff_dir()``, and must not be able to list
    anything else. (Windows has no mode to set -- ``secure_mkdir``'s ``chmod``
    is the documented no-op there -- so the same intent is an NTFS ACL the
    installer writes and ``windows_acl.py`` audits; the mode passed here is
    simply inert on that platform, exactly as it was before this phase.) See
    privilege_separation.py's own module docstring for
    the full layout, and ``handoff_dir()`` below for the files that stay
    reachable from the user's own session.
    """
    override = privilege_separation.data_dir_override()
    if override is not None:
        return secure_mkdir(override, mode=privilege_separation.SYSTEM_ROOT_MODE, foreign_owner_ok=True)
    if is_bundled() or _is_installed_package():
        d = windows_data_dir() if is_windows() else Path.home() / ".privacyfence"
    else:
        d = Path(__file__).parent.parent.parent
    return secure_mkdir(d)


def handoff_dir() -> Path:
    """Where the files the *user's own desktop session* has to reach live:
    the agent's ``mcp_token`` and the ``mcp_url`` the MCPB shim discovers it
    by, the ``web_base_url``/``*_url`` discovery files a human reads, and
    both ends of the Phase 2/3 control channels (``control.sock``,
    ``companion.sock``).

    ``data_dir()`` itself on an ordinary install -- so nothing moves, and no
    caller behaves differently, until privilege separation is opted into.
    On a separated install it's ``data_dir()/handoff``, group-owned by the
    service account with the setgid bit (``3770``) and the installing human
    added to that group, which is what lets the daemon (one account) and the
    companion and agent (another) still hand each other a token and a socket
    while everything else under ``data_dir()`` stays ``0700`` and unreadable
    to them.

    This directory is deliberately *not* a security boundary: ADR 0002
    decision 6 chose to make a minted session insufficient (via #426's
    passkey) rather than to make minting uncallable, precisely because the
    companion and the agent share a uid and no permission bit can separate
    them. What Phase 4 takes away from the agent is ``authority_dir()`` --
    policy, enrolled passkeys, the audit log and its HMAC key -- and that is
    the whole of what it claims.
    """
    if not privilege_separation.is_enabled():
        return data_dir()
    return secure_mkdir(
        data_dir() / privilege_separation.HANDOFF_DIR_NAME,
        mode=privilege_separation.HANDOFF_DIR_MODE,
        foreign_owner_ok=True,
    )


def control_socket_dir() -> Path:
    """Which directory ``web/control_channel.py`` binds ``control.sock`` in.

    ``authority_dir()`` on an ordinary install, unchanged from Phase 2, where
    it sits alongside the ``web_token`` file it replaced. ``handoff_dir()``
    on a separated one, because Phase 4 makes ``authority_dir()`` ``0700``
    under the service account and the companion -- running as the human --
    has to be able to connect. That is not a downgrade of anything Phase 4
    claims: see ``handoff_dir()`` above and ADR 0002 decision 6.
    """
    return handoff_dir() if privilege_separation.is_enabled() else authority_dir()


def org_dir() -> Path:
    """Directory holding the installed organization config bundle."""
    return secure_mkdir(data_dir() / "org")


def user_dir(principal: "Principal | None" = None) -> Path:
    """Per-principal storage root (P6): ``config/settings.yaml``,
    ``credentials/*``, ``logs/audit/*`` and the various per-connector cache
    files all live under here.

    The ``local`` principal's root *is* ``data_dir()`` itself -- not a
    ``users/local/`` subdirectory -- so an existing single-user install
    needs no migration and every path it already has on disk keeps working
    unchanged. Any other principal gets ``data_dir()/users/<id>/``, created
    on demand.

    ``principal`` defaults to ``current_principal()`` -- imported lazily to
    avoid a circular import (principal.py doesn't need paths.py, but nearly
    everything paths.py's callers do need principal.py transitively, so
    importing it at module load time here would risk one on some import
    orders).
    """
    from .principal import LOCAL_PRINCIPAL_ID, current_principal

    if principal is None:
        principal = current_principal()
    if principal.id == LOCAL_PRINCIPAL_ID:
        return data_dir()
    if not _is_safe_principal_id(principal.id):
        raise ValueError(f"Unsafe principal id for filesystem storage: {principal.id!r}")
    return secure_mkdir(data_dir() / "users" / principal.id)


# Relative to a principal's user_dir() -- the pre-#428 location of each of
# these, for both the local principal (whose user_dir() is data_dir() itself)
# and any other one (users/<id>/), which is what makes a single migration
# table below correct for either branch. The audit log is deliberately NOT
# here: unlike the other three, org mode's audit logger never reads it back
# from authority_dir() (audit_log.py's _fallback_log_dir() keeps every
# non-local principal's audit trail at user_dir(principal)/logs/audit,
# unchanged by #428 -- see that function's own docstring). Migrating it
# unconditionally here once broke exactly that: any call to authority_dir()
# for an unrelated reason (e.g. resolving an org principal's settings.yaml)
# silently relocated a directory a *different*, non-redirected code path was
# still actively writing to, losing whatever audit history hadn't been read
# back yet. See _migrate_legacy_audit_log_dir() below for the one caller
# that both migrates and reads audit logs from the new location: the local
# principal's own, in daemon_main.py.
_LEGACY_AUTHORITY_PATHS: tuple[Path, ...] = (
    Path("config") / "settings.yaml",
    Path("webauthn_credentials.json"),
    Path("web_token"),
    Path("web_token_version"),
)

_LEGACY_AUDIT_LOG_RELATIVE = Path("logs") / "audit"

# Per-process memo of which authority_dir() roots have already had their
# migration attempted, so a hot path that calls authority_dir() often (e.g.
# a webauthn check on every step-up) doesn't re-stat these legacy paths on
# every call -- migrating is a one-time, first-startup-after-upgrade thing,
# not a steady-state one. Keyed on the *target* directory rather than a
# bool, since tests exercise more than one principal/data_dir() within a
# single process. Audit-log migration is tracked separately since it's
# opt-in per authority_root() call rather than automatic.
_authority_migration_attempted: set[Path] = set()
_audit_log_migration_attempted: set[Path] = set()


def _migrate_path(legacy: Path, destination: Path) -> None:
    """Move ``legacy`` to ``destination`` if the former exists and the
    latter doesn't -- shared by both the authority-files migration and the
    audit-log one, so the same idempotent, log-and-continue-on-failure
    behavior applies to each: a path that doesn't exist, or a destination
    that already does (including from a previous call), is left alone
    rather than overwritten.

    This is a pure file/directory move, not yet a permissions or ownership
    change -- #428 Phase 4 is what makes ``destination``'s parent
    service-owned. Until then it sits at the same uid as everything else
    under ``legacy``'s own parent.
    """
    if destination.exists() or not legacy.exists():
        return
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(destination)
    except OSError as exc:
        logger.warning("Could not migrate %s to %s: %s", legacy, destination, exc)


def _migrate_legacy_authority_files(root: Path, target: Path) -> None:
    """#428 Phase 1: move a pre-4.1 install's human-authority files -- the
    privacy policy, enrolled WebAuthn credentials, and the web-session
    bootstrap secret -- out of ``root`` and into ``target`` the first time
    ``target`` is asked for, so upgrading doesn't silently reset a
    configured policy or drop enrolled passkeys.
    """
    for relative in _LEGACY_AUTHORITY_PATHS:
        _migrate_path(root / relative, target / relative)


def _migrate_legacy_audit_log_dir(root: Path, target: Path) -> None:
    """Companion to ``_migrate_legacy_authority_files()`` for the one path
    that isn't safe to migrate unconditionally: the audit log. Only ever
    called from ``authority_root(..., migrate_audit_log=True)``, which only
    the local principal's own audit-directory resolution
    (``daemon_main.py``) passes -- the one place that also reads the audit
    log back from ``target`` afterwards. See ``_LEGACY_AUTHORITY_PATHS``'s
    own comment for what goes wrong if this runs for a principal whose
    audit logger doesn't also move.
    """
    _migrate_path(root / _LEGACY_AUDIT_LOG_RELATIVE, target / _LEGACY_AUDIT_LOG_RELATIVE)


def authority_dir(principal: "Principal | None" = None) -> Path:
    """Directory root for the files that back the *human's* authority in
    local mode -- the #428 Phase 2 control channel's socket (macOS/Linux;
    Windows' named pipe lives outside the filesystem, see web/
    control_channel.py -- which is also why Phase 4's Windows layout can keep
    ``handoff_dir()`` read-only to the shared group where POSIX has to make
    it writable), the privacy policy (``config/settings.yaml``), and
    enrolled WebAuthn credentials (P426) -- as distinct from ``user_dir()``,
    which stays reachable by the agent for its own ``mcp_token`` and
    connector caches/credentials.

    Local mode's audit log (plus its HMAC key) is also human-authority state
    and also ends up under this same directory, but it isn't migrated by
    this function -- see ``authority_root()``'s ``migrate_audit_log``
    parameter and ``_LEGACY_AUTHORITY_PATHS``'s own comment for why.

    #428 Phase 1: a pure refactor. This directory lives at the same uid as
    everything else under ``user_dir()`` until #428 Phase 4 moves the
    daemon to its own account and re-owns this subtree to it -- splitting
    the path out now means that later change is a permissions/ownership
    change at one root, not a hunt through every call site that used to
    build one of these paths against ``data_dir()``/``user_dir()`` directly.

    A subdirectory of ``user_dir(principal)``, not a sibling of it: the
    local principal's authority root is ``data_dir()/authority`` (since
    ``user_dir(LOCAL_PRINCIPAL_ID)`` *is* ``data_dir()``), and any other
    principal's is ``user_dir(principal)/authority``. Org mode is out of
    scope for #428 (its daemon already runs where the agent has no access),
    but the non-local branch costs nothing extra to keep correct.
    """
    return authority_root(user_dir(principal))


def authority_root(root: Path, *, migrate_audit_log: bool = False) -> Path:
    """The ``authority`` subdirectory of an arbitrary ``root``, created and
    migrated into exactly like ``authority_dir()`` -- which is
    ``authority_root(user_dir(principal))``, and the function most callers
    actually want.

    Exists as its own function for daemon_main.py's local-principal path
    resolution (``_resolve_authority_path()``, the audit-log directory),
    which anchors on its own ``PROJECT_ROOT``/``data_dir()`` module-level
    references rather than calling ``user_dir()`` -- exactly like the
    pre-#428 ``_resolve_path()`` already did -- so that the tests that
    monkeypatch those two names to sandbox a run keep doing so correctly.
    Both need the identical mkdir-plus-migrate behavior for whatever root
    they resolve to; only which root they start from differs.

    ``migrate_audit_log`` defaults to False: the audit log only migrates
    where it's also read back from the new location afterwards, which today
    is exactly one call site (daemon_main.py's local-principal
    ``init_audit_logger()`` wiring) -- pass True only from there.
    """
    target = root / "authority"
    if target not in _authority_migration_attempted:
        _migrate_legacy_authority_files(root, target)
        _authority_migration_attempted.add(target)
    if migrate_audit_log and target not in _audit_log_migration_attempted:
        _migrate_legacy_audit_log_dir(root, target)
        _audit_log_migration_attempted.add(target)
    return secure_mkdir(target)


def downloads_dir(principal: "Principal | None" = None) -> Path:
    """Per-principal staging area for org-mode download delivery:
    ``user_dir(principal) / "downloads"``, created on demand exactly like
    ``org_dir()``. Holds only
    AES-256-GCM-encrypted ciphertext (download_staging.py's own
    ``DownloadStagingStore`` never derives or stores the decryption key
    anywhere on disk -- see that module's docstring), so this directory's
    contents are worthless without the one-time token that produced them.
    Reuses ``user_dir()``'s own directory-safety logic (``_is_safe_
    principal_id``) rather than adding any new path-construction code
    here."""
    return secure_mkdir(user_dir(principal) / "downloads")


def uploads_dir(principal: "Principal | None" = None) -> Path:
    """Per-principal staging area for the local file bridge's upload side
    (local_files.py, upload_staging.py): ``user_dir(principal) / "uploads"``,
    created on demand exactly like ``downloads_dir()``. Holds only
    AES-256-GCM-encrypted ciphertext (``upload_staging.UploadStagingStore``
    mirrors ``download_staging.DownloadStagingStore``'s "never derive or
    store the decryption key on disk" property), so this directory's
    contents are worthless without the one-time token that produced them.
    Reuses ``user_dir()``'s own directory-safety logic (``_is_safe_
    principal_id``) rather than adding any new path-construction code
    here."""
    return secure_mkdir(user_dir(principal) / "uploads")


def all_uploads_dirs() -> list[Path]:
    """Every upload-staging directory that currently exists on disk, across
    every principal -- the upload-side mirror of ``all_downloads_dirs()``.
    Existence-only: see that function's docstring for why, and for why
    ``upload_staging.UploadStagingStore.__init__`` needs exactly this."""
    base = data_dir()
    dirs = []
    local_uploads = base / "uploads"
    if local_uploads.is_dir():
        dirs.append(local_uploads)
    users_root = base / "users"
    if users_root.is_dir():
        for entry in sorted(users_root.iterdir()):
            if not entry.is_dir() or not _is_safe_principal_id(entry.name):
                continue
            candidate = entry / "uploads"
            if candidate.is_dir():
                dirs.append(candidate)
    return dirs


def all_downloads_dirs() -> list[Path]:
    """Every download-staging directory that currently exists on disk,
    across every principal: the local principal's own ``downloads_dir()``
    plus one per already-provisioned subdirectory of ``data_dir()/users/``.

    Existence-only, unlike ``downloads_dir()``/``user_dir()``: a directory
    that has never been provisioned is simply omitted rather than created,
    so this is safe to call before any principal has ever staged a
    download. ``download_staging.DownloadStagingStore.__init__`` uses this
    to find ciphertext orphaned by a daemon restart -- see that class's
    docstring -- so creating directories here would defeat the point.

    Reuses ``_is_safe_principal_id`` to skip anything under ``users/`` that
    isn't a directory name ``user_dir()`` could itself have produced,
    rather than trusting arbitrary on-disk entries.
    """
    base = data_dir()
    dirs = []
    local_downloads = base / "downloads"
    if local_downloads.is_dir():
        dirs.append(local_downloads)
    users_root = base / "users"
    if users_root.is_dir():
        for entry in sorted(users_root.iterdir()):
            if not entry.is_dir() or not _is_safe_principal_id(entry.name):
                continue
            candidate = entry / "downloads"
            if candidate.is_dir():
                dirs.append(candidate)
    return dirs


def bundle_macos_dir() -> Path | None:
    """Path to Contents/MacOS inside the .app bundle, or None in dev."""
    if is_bundled():
        return Path(sys.executable).parent
    return None


def app_bundle_path() -> Path | None:
    """Path to PrivacyFenceApp.app itself, or None in dev."""
    if is_bundled():
        return Path(sys.executable).parent.parent.parent
    return None
