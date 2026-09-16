"""#428 Phase 4: running local mode's daemon under its own OS account.

Phases 1-3 built everything this needs and deliberately bought no security
with any of it. Phase 1 (``paths.authority_dir()``) collected the files that
back the *human's* authority -- ``config/settings.yaml``, enrolled WebAuthn
credentials, the audit log and its HMAC key -- under one root, so that
re-owning them later would be a permissions change at a single directory
rather than a hunt through every call site. Phase 2 replaced the
``web_token`` file with a socket/pipe control channel. Phase 3 gave the human
a companion app that can speak it. This module is the phase where the uid
finally splits, and all four of issue #428's weaknesses close at once: the
agent runs as the logged-in user, the daemon runs as a dedicated service
account, and the human-authority files are ``0700`` under the latter.

**All three platforms, opt-in on every one.** ``SUPPORTED_PLATFORMS`` is
the single gate, and B5c completes it: an install that has not run its
platform's installer resolves every path in ``paths.py`` exactly as it did
before this module existed, byte for byte, everywhere.

## The layout

The three platforms differ in the names ``PLATFORM_LAYOUTS`` below holds --
where the root is, and what the account and group are called. Everything
else (the three directories, the marker, what goes in ``handoff/``) is
identical, which is the point: one layout, provisioned by whichever
installer a platform has.

===========  ==================================  =============================
Platform     System root                         Service account
===========  ==================================  =============================
macOS        /Library/Application Support/       ``_privacyfence``
             PrivacyFence                        (Apple's hidden
                                                 system-account
                                                 convention)
Linux        /var/lib/privacyfence               ``privacyfence``
             (FHS 3.0 §5.8)                      (no underscore --
                                                 that prefix means
                                                 nothing here)
Windows      %ProgramData%\\PrivacyFence          ``NT SERVICE\\PrivacyFence``
                                                 (a *virtual* account:
                                                 created with the service,
                                                 its own SID, no password
                                                 anyone has to manage)
===========  ==================================  =============================

**What differs on Windows is the primitive, not the layout.** There are no
permission bits there -- ``secure_mkdir``'s ``chmod`` is the documented
no-op ``secure_files.py`` describes -- so the modes below are NTFS ACLs
instead, and ``windows_acl.py`` is both the translation table and the audit.
Two consequences are worth stating rather than leaving to be discovered:

* ``handoff/`` is group-*readable* on Windows, not group-writable. Nothing
  in the user's session has to create anything there, because both control
  channels are named pipes rather than socket files, so the POSIX group's
  ``rwx`` (which ``connect(2)`` on a socket node requires) buys nothing.
* A Windows service runs whatever image its ``binPath`` names, so the
  daemon's own executable becomes part of the boundary: an install the
  logged-in user can rewrite would let the agent run its own code *as the
  service account*. That is why the non-elevated per-user install path
  (#407) cannot be separated, and why ``audit_layout()`` re-checks the image
  on every start -- see ``windows_acl.image_problems()``.

``scripts/macos_privilege_separation.sh enable``,
``scripts/linux_privilege_separation.sh enable`` and
``scripts/windows_privilege_separation.ps1 enable`` are what provision this;
they are the only supported way to turn it on, and they write the marker
file this module reads. Afterwards, taking Linux's root as the example::

    /var/lib/privacyfence                          privacyfence:privacyfence  0711
    ├── privilege-separation.json                  privacyfence:privacyfence  0644
    ├── authority/                                 privacyfence:privacyfence  0700
    │   ├── config/settings.yaml                     <- policy the agent may not edit
    │   ├── webauthn_credentials.json                <- #426's store, now unforgeable
    │   └── logs/audit/                              <- and its HMAC key
    ├── credentials/, logs/, ...                   privacyfence:privacyfence  0700
    └── handoff/                                   privacyfence:privacyfence  2770
        ├── mcp_token, mcp_url                       <- the agent's own credential
        ├── web_base_url, *_url                      <- discovery files a human reads
        ├── control.sock                             <- daemon listens, companion connects
        └── companion.sock                           <- companion listens, daemon connects

The root is ``0711``: traversable by anyone, listable by no one, so a process
running as the logged-in user can reach ``handoff/`` without being able to
enumerate anything else. ``handoff/`` is group-owned by the service group
with the setgid bit, and the human who ran the installer is added to that
group -- that is what lets the companion create its own socket there and the
daemon create ``mcp_token`` there, with both readable by the other and by
nobody else on the machine.

**``handoff/`` is not a security boundary and is not meant to be one.** ADR
0002 decision 6 is explicit that session minting is made *insufficient*
rather than *uncallable*: the companion and the agent run as the same uid, so
no permission bit can tell them apart, and trying would give three platforms
three different strengths of the same claimed guarantee. What moves out of
the agent's reach is ``authority/`` -- policy, passkeys, audit -- and that is
the whole of what Phase 4 claims.

## What this does not defend against

An agent that can obtain root. ``sudo`` re-owns any file and reconfigures any
LaunchDaemon or systemd unit; issue #428's "Honest limits" says so, and so
does ``docs/security-and-compliance.md``. The guarantee is against an agent
running with the user's *normal* privileges, which is the ordinary case, and
it makes escalation require an authentication prompt a human sees.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import shlex
import stat
import subprocess  # nosec B404  # osascript elevation prompt below -- fixed argv, no shell, see that call site
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import secure_files

logger = logging.getLogger(__name__)

# The dedicated account the daemon runs as, per platform. On macOS it is
# underscore-prefixed per Apple's own convention for hidden system accounts
# (``_www``, ``_spotlight``, ...), which is also what keeps it out of the
# login window and Users & Groups. On Linux that prefix means nothing at all
# -- system accounts there are ordinary names distinguished only by their
# sub-``UID_MIN`` id (``useradd --system``) -- so it would read as a typo
# rather than a convention, and the plain name is what every Debian/Fedora
# packaging guide would use.
MACOS_SERVICE_ACCOUNT_NAME = "_privacyfence"
LINUX_SERVICE_ACCOUNT_NAME = "privacyfence"
# Windows has no ``useradd`` equivalent to run, and deliberately needs none:
# a *virtual service account* is created by the Service Control Manager along
# with the service itself, gets its own SID, and has no password for anyone
# (or anything) to store, rotate or leak. Its name is not a choice -- Windows
# derives it from the service's, as ``NT SERVICE\<service name>`` -- which is
# why the two constants below are one fact written twice and
# ``tests/unit/test_privilege_separation.py`` asserts they agree. Preferred
# over ``LocalService`` (#428's own wording) because that account is shared
# with every other service that picked it, so ACLs naming it would grant
# those services access to PrivacyFence's authority files too.
WINDOWS_SERVICE_NAME = "PrivacyFence"
WINDOWS_SERVICE_ACCOUNT_NAME = f"NT SERVICE\\{WINDOWS_SERVICE_NAME}"
# The Windows stand-in for the POSIX service *group*: a local group the
# installing human is added to, named in ``handoff/``'s ACL. A virtual
# service account cannot be given secondary group memberships, so unlike
# macOS/Linux -- where account and group are the same name and the daemon is
# a member of its own group -- the two principals are always listed
# separately in every ACL and pipe DACL this phase writes.
WINDOWS_SERVICE_GROUP_NAME = "PrivacyFenceUsers"
# Windows' own "startup wiring inverts" (ADR 0002): the Scheduled Task the
# installer registers keeps its name and starts the *companion* on a
# separated install, while the daemon becomes the service above. The
# installer script disables the daemon task rather than deleting it, so
# ``disable`` can put it back and the uninstaller's own
# ``schtasks /delete`` still finds it.
WINDOWS_DAEMON_TASK_NAME = "PrivacyFence"
WINDOWS_COMPANION_TASK_NAME = "PrivacyFenceCompanion"

# Where a separated install keeps everything ``paths.data_dir()`` used to put
# under ``~/.privacyfence``. A service account cannot sensibly own something
# inside a human's home directory -- the same reasoning #428 gives for
# Windows having to move out of ``%LOCALAPPDATA%`` into ``%ProgramData%``.
# macOS's is the system-wide twin of the ``~/Library/Application Support``
# every app already uses; Linux's is FHS 3.0 §5.8's ``/var/lib/<package>``,
# "variable state information" a program modifies as it runs, which is
# exactly what this directory is.
MACOS_SYSTEM_ROOT = Path("/Library/Application Support/PrivacyFence")
LINUX_SYSTEM_ROOT = Path("/var/lib/privacyfence")
# Windows' is ``%ProgramData%``: per-machine application state, outside every
# user profile, which is precisely what ``%LOCALAPPDATA%`` is not. The
# literal below is the default every supported Windows install actually has;
# ``system_root()`` prefers the environment variable when it is set, because
# a machine can be built with ``%ProgramData%`` redirected to another volume
# and a hardcoded ``C:`` would then name a directory nothing else uses. Kept
# as a forward-slash ``Path`` so it compares equal to the same path built
# anywhere else -- ``PureWindowsPath`` normalizes separators, and the MCPB
# shim's own copy of this table is a POSIX-style string for the same reason.
WINDOWS_PROGRAM_DATA_ENV_VAR = "ProgramData"
WINDOWS_SYSTEM_ROOT = Path("C:/ProgramData/PrivacyFence")

# Written by the installer, read by every PrivacyFence process (daemon,
# companion, and anything the human runs from a shell) so all of them agree
# on the layout without being passed any configuration. It lives *inside*
# the root it describes and is world-readable: its contents are account and
# directory names, not secrets, and a user-session process that cannot read
# it cannot tell that separation is on at all.
MARKER_FILE_NAME = "privilege-separation.json"
MARKER_VERSION = 1

# Test/development escape hatch, not something a real install sets: relocates
# the whole separated layout, marker included, so a test can exercise this
# against a ``tmp_path`` instead of a directory that needs root to create.
# Every process of one install has to agree on it, which is exactly why a
# real install uses the default and passes nothing.
SYSTEM_ROOT_ENV_VAR = "PRIVACYFENCE_SYSTEM_ROOT"

HANDOFF_DIR_NAME = "handoff"

# Modes the installer sets and ``paths.py`` re-asserts on every resolution
# (``secure_mkdir``'s own self-healing posture, see secure_files.py). Named
# here rather than spelled inline in three places so the installer script,
# the path helpers and the audit below cannot drift apart -- and so
# ``tests/unit/test_privilege_separation.py`` can assert the shell script
# agrees with them.
SYSTEM_ROOT_MODE = 0o711
HANDOFF_DIR_MODE = 0o2770
AUTHORITY_DIR_MODE = 0o700
SOCKET_MODE_SEPARATED = 0o660
SOCKET_MODE_SHARED_UID = 0o600
# Files *inside* handoff/. 0640 rather than the 0600 every other credential
# in this codebase gets: the whole point of that directory is that the two
# accounts can read each other's files, and ``mcp_token`` in particular has
# to stay readable by the agent -- it is the agent's own credential, and
# #428 keeps it deliberately reachable ("the point is to stop one uid holding
# both sides", not to take the agent's own side away). Group-readable, never
# group-writable: nothing in the user's session needs to *rewrite* what the
# daemon publishes there.
HANDOFF_FILE_MODE_SEPARATED = 0o640


@dataclass(frozen=True)
class PlatformLayout:
    """The three names, and the two commands, that differ between platforms.

    Everything *else* about a separated install -- the directory structure,
    the modes above, the marker's own format -- is identical everywhere, so
    this is deliberately the whole of the per-platform surface. Adding a
    platform means one more entry here plus the installer that can provision
    it -- which is exactly what B5c turned out to be, plus the ACL work
    ``windows_acl.py`` holds, because Windows' permissions are not a mode.
    """

    system_root: Path
    service_account: str
    service_group: str
    #: Repo-relative path of the script that provisions and audits it. Not
    #: what the errors below quote -- see ``status_command`` -- but what the
    #: contract test resolves to check that the two halves still agree.
    installer: str
    #: How to *inspect* a separated install, as a human would actually type
    #: it. Deliberately not ``installer`` verbatim: on Linux the .deb puts
    #: that same script on PATH as ``privacyfence-privilege-separation``, and
    #: most Linux installs are the .deb, so quoting a repo-relative path at
    #: someone reading a daemon log would name a file they do not have.
    status_command: str
    #: How the daemon is *supposed* to be started on a separated install --
    #: the thing to do instead of whatever produced a wrong-account process.
    start_command: str


# #428 P4 ships per platform (B5a/B5b/B5c) rather than as one "x3 platforms"
# phase, so macOS and Linux could land and soak even if Windows' net-new ACL
# work runs long. A platform is supported exactly when it has an entry here,
# and an entry without a matching installer would make every process on that
# platform look for a marker nothing can write.
PLATFORM_LAYOUTS: dict[str, PlatformLayout] = {
    "darwin": PlatformLayout(
        system_root=MACOS_SYSTEM_ROOT,
        service_account=MACOS_SERVICE_ACCOUNT_NAME,
        service_group=MACOS_SERVICE_ACCOUNT_NAME,
        installer="scripts/macos_privilege_separation.sh",
        status_command="sudo scripts/macos_privilege_separation.sh status",
        start_command="sudo launchctl kickstart -k system/com.privacyfence.daemon",
    ),
    "linux": PlatformLayout(
        system_root=LINUX_SYSTEM_ROOT,
        service_account=LINUX_SERVICE_ACCOUNT_NAME,
        service_group=LINUX_SERVICE_ACCOUNT_NAME,
        installer="scripts/linux_privilege_separation.sh",
        status_command="sudo privacyfence-privilege-separation status",
        start_command="sudo systemctl restart privacyfence-daemon.service",
    ),
    "win32": PlatformLayout(
        system_root=WINDOWS_SYSTEM_ROOT,
        service_account=WINDOWS_SERVICE_ACCOUNT_NAME,
        service_group=WINDOWS_SERVICE_GROUP_NAME,
        installer="scripts/windows_privilege_separation.ps1",
        # What a human types, which on Windows is never the repo-relative
        # path: the installer copies this script next to the application as
        # ``privilege-separation.ps1``, and PowerShell will not run an
        # unsigned script from disk without being told to. Quoting the
        # elevated, real-install form is the only version that works when
        # pasted out of a daemon log by someone who has no checkout.
        #
        # ``$env:ProgramFiles``, not ``%ProgramFiles%``: this is a PowerShell
        # command and PowerShell does not expand the ``%VAR%`` form, so the
        # cmd.exe spelling would resolve to a literal directory name that
        # does not exist -- in the one shell the reader has just been told to
        # open elevated. The same spelling is what README.md and
        # docs/platform-support.md quote.
        status_command=(
            'powershell -ExecutionPolicy Bypass -File '
            '"$env:ProgramFiles\\PrivacyFence\\privilege-separation.ps1" status'
        ),
        start_command=f"sc.exe start {WINDOWS_SERVICE_NAME}",
    ),
}

SUPPORTED_PLATFORMS = tuple(PLATFORM_LAYOUTS)


class PrivilegeSeparationError(RuntimeError):
    """Raised by ``check_runtime_identity()`` when this install is separated
    but this process is not the account that is supposed to be holding the
    daemon's half of it -- see that function for why that has to be fatal
    rather than a warning."""


@dataclass(frozen=True)
class Separation:
    """A parsed, validated marker file: what the installer provisioned."""

    version: int
    platform: str
    service_account: str
    service_group: str
    owner_user: str
    enabled_at: str
    # Derived from where the marker itself was found rather than stored in
    # it, so the file can never disagree with its own location.
    data_dir: Path

    @property
    def handoff_dir(self) -> Path:
        return self.data_dir / HANDOFF_DIR_NAME

    @property
    def authority_dir(self) -> Path:
        return self.data_dir / "authority"


def current_platform() -> str:
    """Indirection around ``sys.platform`` for exactly the reason
    ``paths.is_windows()`` has one around ``os.name``: a test needs to ask
    "what would a macOS install do?" while running on this repo's Ubuntu CI,
    and monkeypatching ``sys.platform`` itself is not safe for the rest of a
    process that keeps consulting it."""
    return sys.platform


def platform_layout() -> PlatformLayout | None:
    """This platform's entry in ``PLATFORM_LAYOUTS``, or None on one #428 P4
    hasn't shipped for yet."""
    return PLATFORM_LAYOUTS.get(current_platform())


def system_root() -> Path | None:
    """Where a separated install's root *would* be on this platform, whether
    or not one has been provisioned -- None on a platform #428 P4 hasn't
    shipped for yet, which is what makes every other function here a cheap
    no-op there."""
    override = os.environ.get(SYSTEM_ROOT_ENV_VAR)
    if override:
        # An override that isn't absolute would resolve differently per
        # process depending on each one's cwd -- the daemon's is set by its
        # LaunchDaemon/systemd unit, the companion's by whatever launched it.
        # Rejecting it outright beats half the install silently using a
        # different root.
        candidate = Path(override)
        if not candidate.is_absolute():
            logger.warning(
                "%s=%r is not an absolute path -- ignoring it and using the default layout.",
                SYSTEM_ROOT_ENV_VAR, override,
            )
        else:
            return candidate
    layout = platform_layout()
    if layout is None:
        return None
    if current_platform() == "win32":
        # ``%ProgramData%`` is ``C:\ProgramData`` on every ordinary install
        # and is what ``WINDOWS_SYSTEM_ROOT`` already spells, so this branch
        # normally changes nothing. It exists for the machine where that
        # folder has been redirected: the installer's own ``icacls`` runs
        # against the redirected path, so resolving the hardcoded one here
        # would have every process looking somewhere the installer never
        # provisioned. Missing entirely (a stripped-down service
        # environment) falls back to the literal, the same way
        # ``paths.windows_data_dir()`` falls back for ``%LOCALAPPDATA%``.
        program_data = os.environ.get(WINDOWS_PROGRAM_DATA_ENV_VAR)
        if program_data:
            return Path(program_data) / "PrivacyFence"
    return layout.system_root


def marker_path() -> Path | None:
    root = system_root()
    return None if root is None else root / MARKER_FILE_NAME


# Read once per process and cached: ``paths.data_dir()`` is called often
# enough (every credential read, every settings resolution) that re-stat'ing
# and re-parsing a JSON file on each one would be waste, and the answer
# cannot change under a running process anyway -- enabling or disabling
# separation stops and restarts the daemon. ``reset_cache()`` exists for
# tests, which do change it mid-process; tests/conftest.py calls it between
# tests so one test's marker never leaks into the next.
_CACHE_UNSET = object()
_cached_separation: object = _CACHE_UNSET


def reset_cache() -> None:
    global _cached_separation
    _cached_separation = _CACHE_UNSET


def _parse_marker(path: Path) -> Separation | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        # EACCES here is not an error to report: on a *non*-separated install
        # the file simply doesn't exist, and on a separated one it's 0644, so
        # the only way to land here is a hand-edited layout.
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR):
            logger.warning("Could not read the privilege-separation marker at %s: %s", path, exc)
        return None
    except ValueError as exc:
        logger.error("Ignoring a malformed privilege-separation marker at %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.error("Ignoring a malformed privilege-separation marker at %s: not a JSON object", path)
        return None
    version = raw.get("version")
    if version != MARKER_VERSION:
        # A marker from a *newer* PrivacyFence describes a layout this build
        # may not know how to read. Falling back to the unseparated layout
        # would silently start writing policy to a second, agent-writable
        # settings.yaml, so refuse to interpret it instead and let
        # check_runtime_identity() turn that into a startup failure.
        logger.error(
            "Privilege-separation marker at %s has version %r, expected %r -- ignoring it.",
            path, version, MARKER_VERSION,
        )
        return None
    try:
        return Separation(
            version=MARKER_VERSION,
            platform=str(raw["platform"]),
            service_account=str(raw["service_account"]),
            service_group=str(raw["service_group"]),
            owner_user=str(raw["owner_user"]),
            enabled_at=str(raw.get("enabled_at", "")),
            data_dir=path.parent,
        )
    except KeyError as exc:
        logger.error("Privilege-separation marker at %s is missing %s -- ignoring it.", path, exc)
        return None


def separation() -> Separation | None:
    """This install's separated layout, or None if it has none -- the one
    function every other check here (and all of ``paths.py``'s branching) is
    built on."""
    global _cached_separation
    if _cached_separation is not _CACHE_UNSET:
        return _cached_separation  # type: ignore[return-value]
    path = marker_path()
    result = None if path is None else _parse_marker(path)
    if result is not None and result.platform != current_platform():
        # A marker copied (or a volume moved) between platforms. Its account
        # names mean nothing here.
        logger.error(
            "Privilege-separation marker at %s was written for platform %r, not %r -- ignoring it.",
            path, result.platform, current_platform(),
        )
        result = None
    _cached_separation = result
    return result


def is_enabled() -> bool:
    return separation() is not None


def data_dir_override() -> Path | None:
    """``paths.data_dir()``'s hook: the separated root, or None to leave that
    function's own ``~/.privacyfence``/``%LOCALAPPDATA%``/source-checkout
    resolution exactly as it was."""
    state = separation()
    return None if state is None else state.data_dir


def socket_mode() -> int:
    """The mode ``web/control_channel.py`` binds its unix sockets at. ``0600``
    normally -- daemon, companion and agent are one uid, so nothing else could
    connect anyway. ``0660`` when separated, because the two ends are now two
    accounts and the shared ``_privacyfence`` group is what lets them still
    reach each other. Deliberately *not* narrower than that: ADR 0002 decision
    6 chose to make a session insufficient rather than to make minting
    uncallable, so this channel is not where the boundary is drawn."""
    return SOCKET_MODE_SEPARATED if is_enabled() else SOCKET_MODE_SHARED_UID


def handoff_dir_mode() -> int:
    """The mode ``paths.handoff_dir()`` is kept at -- ``2770`` when separated
    (setgid so the daemon and the companion keep producing group-owned files
    for each other regardless of which one creates them), and the ordinary
    ``0700`` otherwise, where ``handoff_dir()`` *is* ``data_dir()`` and this
    must not change it."""
    return HANDOFF_DIR_MODE if is_enabled() else secure_files.DEFAULT_DIR_MODE


def handoff_file_mode() -> int:
    return HANDOFF_FILE_MODE_SEPARATED if is_enabled() else secure_files.DEFAULT_FILE_MODE


def write_handoff_file(path: Path, text: str) -> None:
    """``atomic_write_text`` with both modes a file under
    ``paths.handoff_dir()`` needs -- the file's own, and (critically) the
    *directory's*, since ``secure_mkdir`` re-asserts that on an existing
    directory and would otherwise re-tighten the shared handoff directory to
    ``0700`` on every single discovery-file write, locking the agent and the
    companion out one write after the installer had let them in.

    Byte-identical to a plain ``atomic_write_text(path, text)`` on an
    unseparated install."""
    secure_files.atomic_write_text(
        path, text, mode=handoff_file_mode(), dir_mode=handoff_dir_mode(),
    )


def ensure_handoff_file_mode(path: Path) -> None:
    """Re-assert ``handoff_file_mode()`` on a file that already existed --
    ``secure_mkdir``'s own self-healing posture, applied to the one handoff
    file that is deliberately *not* rewritten on every daemon start:
    ``mcp_token`` is reused across restarts, so a token migrated in from a
    pre-Phase-4 install would keep its old ``0600`` forever and the agent
    would never be able to read its own credential again. Best-effort and
    silent on failure, like every other permission fix-up here.

    A no-op on Windows, where there is no mode to re-assert and the
    equivalent problem is solved a different way: a file *created* in
    ``handoff/`` inherits that directory's ACL, and a file *moved* there by
    the migration keeps whatever ACL it had in ``%LOCALAPPDATA%``, so
    ``scripts/windows_privilege_separation.ps1`` runs ``icacls /reset /t``
    over the directory once, at enable time, rather than leaving every
    process to re-derive an inherited ACL it has no way to compute.
    """
    if os.name == "nt":  # pragma: no cover -- exercised by the platform-windows job
        return
    try:
        if path.stat().st_mode & 0o7777 != handoff_file_mode():
            path.chmod(handoff_file_mode())
    except OSError as exc:  # pragma: no cover -- best effort, same posture as secure_mkdir
        logger.warning("Could not set permissions on %s: %s", path, exc)


def current_user_name() -> str:
    """This process's account name, or its numeric uid as a string where
    ``pwd`` can't answer (a uid with no passwd entry -- possible inside a
    container).

    Windows answers from the process token rather than from ``%USERNAME%``,
    which #428 P4's Windows phase made necessary rather than merely tidier:
    a service running under the virtual ``NT SERVICE\\PrivacyFence`` account
    is handed an environment block whose ``USERNAME`` is the *machine*
    account, so the env var would report a mismatch for the one process that
    is running as exactly the right account. See
    ``windows_acl.current_account_name()``; the env var stays as the
    fallback for a build with no pywin32 at all, where it is still right for
    an ordinary interactive process.
    """
    if os.name == "nt":  # pragma: no cover -- exercised by the platform-windows job
        from . import windows_acl

        try:
            return windows_acl.current_account_name()
        except Exception:
            return os.environ.get("USERNAME", "")
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:  # pragma: no cover -- a uid with no passwd entry
        return str(os.geteuid())


def accounts_equal(left: str, right: str) -> bool:
    """Whether two account names name the same account.

    Case-sensitive on POSIX, where they are, and case-*insensitive* on
    Windows, where they are not: ``NT SERVICE\\PrivacyFence`` and
    ``NT Service\\privacyfence`` are one account, and ``LookupAccountSid``
    is free to return either spelling depending on how the SID was
    registered. Comparing those with ``==`` would make
    ``check_runtime_identity()`` refuse to start a correctly separated
    daemon.
    """
    if current_platform() == "win32":
        from . import windows_acl

        return windows_acl.normalize_trustee(left) == windows_acl.normalize_trustee(right)
    return left == right


def running_as_service_account() -> bool:
    state = separation()
    return state is not None and accounts_equal(current_user_name(), state.service_account)


def check_runtime_identity() -> None:
    """Daemon startup's gate: refuse to run as the wrong account on an
    install that has been separated.

    This is the one place in local mode that fails closed rather than
    warning, and the reason is that the failure is silent and destructive
    otherwise. ``authority/`` is ``0700`` under ``_privacyfence``; a daemon
    started as the logged-in user cannot read ``config/settings.yaml``
    through it, and ``daemon_main.load_config()``'s own first-run behavior is
    to seed a fresh default from the packaged example. So the visible symptom
    of "the LaunchDaemon didn't take and the old LaunchAgent started it
    instead" would be a policy reset -- every always-allow rule and PII
    setting silently back to defaults, with the real ones still on disk and
    unread. Refusing to start turns that into a log line and a service that
    is plainly down.

    Also refuses for a marker this build could not parse (a future
    ``version``, a foreign ``platform``): ``separation()`` returns None for
    those, so ``paths.py`` would resolve the *unseparated* layout while a
    separated one sits on disk -- the same policy-reset outcome by a
    different route. Checking the marker file's mere existence here, rather
    than the parsed result, is what distinguishes it from a genuinely
    unseparated install.
    """
    layout = platform_layout()
    state = separation()
    if state is None:
        path = marker_path()
        if path is not None and path.exists():
            how_to_inspect = (
                f" Run '{layout.status_command}' to inspect the install."
                if layout is not None
                else ""
            )
            raise PrivilegeSeparationError(
                f"{path} exists but could not be interpreted by this version of PrivacyFence "
                "(see the errors logged above). Refusing to start: continuing would resolve the "
                "un-separated data directory and silently ignore the policy, passkeys and audit "
                f"log stored under the separated one.{how_to_inspect}"
            )
        return
    actual = current_user_name()
    if not accounts_equal(actual, state.service_account):
        how_to_start = (
            f" Start the daemon via its service ('{layout.start_command}') rather than directly."
            if layout is not None
            else ""
        )
        # Explicitly quoted rather than ``!r``: every account name here is a
        # Windows one on B5c's platform (``NT SERVICE\PrivacyFence``), and
        # repr would double each backslash -- so the message would name an
        # account that does not exist for the one reader most likely to paste
        # it into a command.
        raise PrivilegeSeparationError(
            f"This install runs the daemon under the dedicated '{state.service_account}' account "
            f"(#428 Phase 4), but this process is running as '{actual}'. Refusing to start: the "
            f"human-authority files under {state.authority_dir} are not readable as '{actual}', so "
            f"starting would seed a fresh default policy and ignore the real one.{how_to_start}"
        )


def _describe_mode(path: Path, expected: int) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    actual = stat.S_IMODE(st.st_mode)
    if actual == expected:
        return None
    return f"{path} is mode {actual:04o}, expected {expected:04o}"


def audit_layout() -> list[str]:
    """One human-readable problem per way this install's on-disk layout has
    drifted from what the installer provisioned -- the separated counterpart
    of ``secure_files.audit_directory_permissions()``, which cannot be used
    here because two of these three directories are *deliberately* not
    ``0700`` (see the layout in this module's docstring).

    Empty list on an unseparated install, so ``daemon_main``'s own SEC-09
    startup check can call it unconditionally. Best-effort like every other
    permission check in this codebase: a path that can't be ``stat``'d is
    skipped rather than reported, since the process doing the checking may
    legitimately not be able to see it.
    """
    state = separation()
    if state is None:
        return []
    if current_platform() == "win32":
        return windows_layout_problems(state)
    problems = []
    for path, expected in (
        (state.data_dir, SYSTEM_ROOT_MODE),
        (state.authority_dir, AUTHORITY_DIR_MODE),
        (state.handoff_dir, HANDOFF_DIR_MODE),
    ):
        problem = _describe_mode(path, expected)
        if problem is not None:
            problems.append(problem)
    owner_problem = _authority_owner_problem(state)
    if owner_problem is not None:
        problems.append(owner_problem)
    return problems


def daemon_image_paths() -> tuple[Path, ...]:
    """The files a Windows service's ``binPath`` actually executes, for the
    one check with no POSIX counterpart (``windows_acl.image_problems()``).

    Two paths, not one, because there are two ways to replace what a service
    runs: rewriting the executable itself, and dropping a DLL next to it for
    the loader to find first. Both are "write access to the install
    directory", so both are checked.

    Empty on anything but a frozen build. A source or ``pip`` install's
    ``sys.executable`` is the Python interpreter, which is shared with every
    other Python program on the machine and is not something this install
    provisioned or can speak about -- reporting a user-writable
    ``python.exe`` as a PrivacyFence layout defect would be noise on every
    developer's own machine, which is where unfrozen installs actually live.
    """
    if not (getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")):
        return ()
    image = Path(sys.executable)
    return (image, image.parent)


def windows_layout_problems(state: Separation) -> list[str]:
    """``audit_layout()``'s Windows half: the same three directories, read
    as NTFS ACLs rather than as mode bits, plus the daemon's own image.

    Structured exactly like the POSIX half -- one human-readable string per
    way the install has drifted from what the installer provisioned, and a
    path that cannot be read is skipped rather than reported, since the
    process doing the checking may legitimately not be able to see it. The
    one addition is ``has_null_dacl()``: a directory with no DACL at all
    grants every account full control, which reads as "could not check"
    everywhere else and has to be called out explicitly here.
    """
    from . import windows_acl

    checks: tuple[tuple[Path, Any, dict[str, str]], ...] = (
        (state.data_dir, windows_acl.root_problems, {}),
        (state.authority_dir, windows_acl.authority_problems, {}),
        (
            state.handoff_dir,
            windows_acl.handoff_problems,
            {"service_group": state.service_group},
        ),
    )
    problems: list[str] = []
    for path, check, extra in checks:
        # Read before the DACL and passed into every check: an object's
        # owner holds WRITE_DAC implicitly, so it decides both whether the
        # ACL means anything at all (owner_problems) and what an OWNER
        # RIGHTS ACE inside it actually grants (windows_acl.
        # effective_trustee).
        owner = windows_acl.read_owner(path)
        problems.extend(
            windows_acl.owner_problems(path, owner, service_account=state.service_account)
        )
        aces = windows_acl.read_dacl(path)
        if aces is None:
            if windows_acl.has_null_dacl(path):
                problems.append(
                    f"{path} has no access-control list at all, which grants every account on "
                    "this machine full control over it."
                )
            continue
        problems.extend(
            check(path, aces, service_account=state.service_account, owner=owner, **extra)
        )
    for image in daemon_image_paths():
        aces = windows_acl.read_dacl(image)
        if aces is not None:
            problems.extend(
                windows_acl.image_problems(image, aces, service_account=state.service_account)
            )
    return problems


def windows_channel_trustees() -> tuple[str, ...]:
    """The accounts a control-channel named pipe's DACL has to grant, on top
    of the process's own (``web/control_channel.py``).

    Empty on an unseparated install, where both ends of both channels are
    the same account and the existing "this user's SID and nothing else"
    descriptor already says everything there is to say. On a separated one
    the daemon and the companion are two accounts, so each end names both
    -- the Windows counterpart of ``socket_mode()``'s ``0660``, and
    deliberately not narrower for the same reason: ADR 0002 decision 6 draws
    the boundary at ``authority/``, not here.

    Both principals are listed explicitly rather than relying on the group
    alone, because a virtual service account cannot be made a member of a
    local group -- see ``WINDOWS_SERVICE_GROUP_NAME``.
    """
    state = separation()
    if state is None or current_platform() != "win32":
        return ()
    return (state.service_account, state.service_group)


def _authority_owner_problem(state: Separation) -> str | None:
    """The check that actually matters: ``authority/`` being ``0700`` buys
    nothing if it is ``0700`` *under the logged-in user's own uid*, which is
    precisely the pre-#428 state this phase exists to leave behind."""
    if os.name == "nt":  # pragma: no cover -- exercised by the platform-windows job
        # Windows has no file owner to compare, and its equivalent of this
        # check -- "is authority/ reachable by anything but the service
        # account" -- is an ACL question rather than an ownership one. See
        # windows_layout_problems(), which audit_layout() branches to before
        # ever reaching here.
        return None
    import pwd

    try:
        st = state.authority_dir.stat()
    except OSError:
        return None
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:  # pragma: no cover -- a uid with no passwd entry
        owner = str(st.st_uid)
    if owner == state.service_account:
        return None
    return (
        f"{state.authority_dir} is owned by {owner!r}, not by the dedicated "
        f"{state.service_account!r} account -- the privilege separation this install "
        "advertises is not actually in effect."
    )


# ── #428 D1 (4.1): auto-enable trigger ────────────────────────────────────────
#
# D1 was the original plan's "flip the default on, once a platform's opt-in
# has soaked through a full release cycle" step, deferred to 4.2. That soak
# period is explicitly overridden for 4.1: macOS and Linux both auto-enable
# now, macOS from here and Linux from debian/postinst (root already, at
# package-configure time, no prompt needed). Windows has no B5c yet, so
# nothing here touches it -- SUPPORTED_PLATFORMS still gates everything else
# in this module the same way it always has.
#
# macOS has no package-manager postinst to lean on the way the .deb does: a
# DMG install is a drag to /Applications, nothing runs as root at install
# time, and nothing short of a human answering an admin password prompt can
# create a system account or a LaunchDaemon. This is that prompt, asked once.
AUTO_ENABLE_ATTEMPTED_MARKER_NAME = ".separation_auto_enable_attempted"


def _macos_installer_script_path() -> Path | None:
    """Where ``scripts/macos_privilege_separation.sh`` actually is for *this*
    running process: bundled into the .app's ``Resources/`` for a packaged
    install (``scripts/build_dmg.sh`` copies both it and its launchd
    templates there, preserving the repo's own ``scripts/`` +
    ``installer/macos/`` sibling layout so the script's own ``REPO_ROOT``
    resolution needs no packaged-vs-checkout branch), or the repo-relative
    checkout path for a source install. ``None`` if neither exists -- the
    normal case in a test process, and treated the same as "nothing to
    auto-enable with"."""
    from . import paths

    bundle = paths.app_bundle_path()
    if bundle is not None:
        candidate = bundle / "Contents" / "Resources" / "scripts" / "macos_privilege_separation.sh"
    else:
        candidate = Path(__file__).resolve().parents[2] / "scripts" / "macos_privilege_separation.sh"
    return candidate if candidate.is_file() else None


def _applescript_quoted(text: str) -> str:
    """Escape ``text`` for a double-quoted AppleScript string literal
    (``\\`` and ``"`` are the only two characters that mean anything there).
    ``text`` here is already a full shell command built with
    ``shlex.quote()`` per argument -- that is the shell layer's own quoting,
    this is the AppleScript layer's on top of it, and neither substitutes
    for the other."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def maybe_auto_enable_macos() -> None:
    """#428 D1 (4.1): on an unseparated macOS install, ask once -- via the
    standard macOS admin-password dialog -- to run
    ``scripts/macos_privilege_separation.sh enable`` on this human's behalf.

    Called from ``daemon_main.main()``, after ``check_runtime_identity()``
    and only on the path that starts the persistent daemon. A no-op on every
    other platform, on an already-separated install, and when the script
    this needs isn't packaged into the running app (a test process, or a
    source checkout run without ``scripts/`` next to it).

    Fires at most once per install: a marker file next to the (still
    unseparated) data directory records the attempt regardless of whether
    the human approves the prompt or cancels it, so a decline is respected
    rather than repeated at the next daemon start. There is no UI to ask
    again short of running the script by hand or deleting that marker --
    same as every other platform, where opting in has only ever been that
    one manual command.

    Runs the elevation prompt on a background thread so daemon startup never
    blocks on a human answering (or ignoring) a password dialog, and treats
    every failure as non-fatal: this is a convenience layered on top of the
    opt-in path, never a replacement for it, and it must never take the
    daemon down with it.
    """
    if current_platform() != "darwin":
        return
    if is_enabled():
        return
    script = _macos_installer_script_path()
    if script is None:
        return

    from . import paths

    marker = paths.data_dir() / AUTO_ENABLE_ATTEMPTED_MARKER_NAME
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("attempted\n", encoding="utf-8")
    except OSError:
        logger.warning("could not write %s -- skipping the auto-enable prompt this run", marker, exc_info=True)
        return

    threading.Thread(
        target=_run_auto_enable_macos,
        args=(script,),
        name="privilege-separation-auto-enable",
        daemon=True,
    ).start()



# Absolute path, not "osascript" on PATH: bandit B607 flags a partial
# executable path as attacker-PATH-controllable, and macOS ships this at a
# fixed location -- no reason to resolve it any other way.
_OSASCRIPT = "/usr/bin/osascript"


def _run_auto_enable_macos(script: Path) -> None:
    command = f"{shlex.quote(str(script))} enable --auto"
    applescript = f"do shell script {_applescript_quoted(command)} with administrator privileges"
    try:
        result = subprocess.run(  # nosec B603  # fixed argv list below, no shell, nothing here is attacker-controlled
            [_OSASCRIPT, "-e", applescript],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("automatic privilege-separation enable did not run", exc_info=True)
        return
    if result.returncode != 0:
        # osascript's own exit code for a declined password prompt -- an
        # expected outcome, not a bug. `enable --auto` exits 0 even when it
        # skips itself (see that script's own --auto handling), so a nonzero
        # code here means osascript couldn't run it at all, which is worth a
        # log line either way.
        logger.info("automatic privilege-separation enable did not complete: %s", result.stderr.strip())
        return
    reset_cache()
    logger.info("privilege separation enabled automatically (#428 D1) -- restart the daemon to pick it up")
