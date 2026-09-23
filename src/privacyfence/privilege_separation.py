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
  service account*. That is why ``enable`` refuses such an install outright
  and why ``audit_layout()`` re-checks the image on every start -- see
  ``windows_acl.image_problems()``. It is also why there is only one Windows
  install tier: ADR 0003 decision 4 withdrew #407's non-elevated per-user
  one, which by construction could never satisfy this. macOS has the same
  exposure by a different route: ``/Applications`` is ``root:admin
  drwxrwxr-x`` and a drag-installed ``.app`` is normally owned by the
  installing user, so nothing about a packaged macOS install makes the
  daemon's image root-owned on its own (B1) -- ``_posix_image_problems()``
  is that platform's counterpart, a ``stat`` walk rather than an ACL read.

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
    └── handoff/                                   privacyfence:privacyfence  3770
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

import contextlib
import errno
import json
import logging
import os
import shlex
import shutil
import stat
import subprocess  # nosec B404  # osascript elevation prompt below -- fixed argv, no shell, see that call site
import sys
import tempfile
from collections.abc import Iterator
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
#
# B11: the daemon's own environment is controlled by launchd/systemd, but the
# companion and the MCPB shim honour this var too, and *their* environment is
# whatever the logged-in user's session set -- on a genuinely separated
# install, that is exactly the boundary this module exists to hold. So
# system_root() below only honours it when the platform's real default root
# has no marker of its own; once a real install is provisioned there, a
# user-session process redirecting itself elsewhere is not a test, it's the
# attack.
SYSTEM_ROOT_ENV_VAR = "PRIVACYFENCE_SYSTEM_ROOT"

HANDOFF_DIR_NAME = "handoff"

# Modes the installer sets and ``paths.py`` re-asserts on every resolution
# (``secure_mkdir``'s own self-healing posture, see secure_files.py). Named
# here rather than spelled inline in three places so the installer script,
# the path helpers and the audit below cannot drift apart -- and so
# ``tests/unit/test_privilege_separation.py`` can assert the shell script
# agrees with them.
SYSTEM_ROOT_MODE = 0o711
# The local-mode-fixes plan's interim multi-user guard (Phase 2 §2.6): the
# leading ``3`` is ``01000`` (the sticky bit) on top of the setgid ``02000``
# this already
# carried -- the filesystem-level half of the companion-socket-takeover fix
# ``web/control_channel.py``'s ``_existing_socket_owner_problem()`` is the
# code-level half of. Without it, any member of the service group (every
# account this install has been extended to) can unlink another member's
# ``companion.sock`` even though the group only grants ``rwx`` on the
# directory, not ownership of what is in it -- the same reason ``/tmp`` has
# carried this bit since 4.3BSD. With it, only the file's own owner (or
# root) may remove or rename an entry here, so the code-level check is
# belt, this is braces: a socket the code-level check would refuse to
# rebind now also can't be deleted out from under a still-running owner by
# anything but that owner.
HANDOFF_DIR_MODE = 0o3770
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
    #: How to stop the daemon on a separated install -- what the control
    #: channel's own ``QUIT`` refuses in its place (#428 Phase 4 / B4):
    #: once the daemon is a system service, only its service manager gets
    #: to stop it, not a line on a socket the agent shares a group with.
    stop_command: str
    #: ADR 0003 decision 6: what ``enforce_separation()`` names when a
    #: packaged, unseparated install refuses to serve -- the one command
    #: that fixes it, in the elevated form a human would actually type
    #: rather than ``installer`` verbatim (see that field's own docstring
    #: for why the two differ).
    enable_command: str
    #: The local-mode-fixes plan's Phase 2 (companion-as-daemon-manager): the
    #: *unprivileged* argv that reads this platform's service-manager state
    #: without asking for a password -- ``daemon_status.probe()``'s fallback
    #: once the control
    #: channel itself doesn't answer. Not ``status_command`` above, which is
    #: what a human types (and which needs ``sudo``/an elevated shell only
    #: because the *script's* own ``status`` prints the on-disk layout audit,
    #: not because reading service state needs privilege -- ``launchctl
    #: print``/``systemctl show``/``sc query`` all work for an ordinary
    #: session). Named here, once, rather than spelled inline in
    #: ``daemon_status.py``, so that module and the platform scripts'
    #: own ``daemon status`` subcommand can't drift apart on which service
    #: name they mean.
    daemon_ctl_argv: tuple[str, ...]


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
        stop_command="sudo launchctl bootout system/com.privacyfence.daemon",
        enable_command="sudo scripts/macos_privilege_separation.sh enable",
        daemon_ctl_argv=("launchctl", "print", "system/com.privacyfence.daemon"),
    ),
    "linux": PlatformLayout(
        system_root=LINUX_SYSTEM_ROOT,
        service_account=LINUX_SERVICE_ACCOUNT_NAME,
        service_group=LINUX_SERVICE_ACCOUNT_NAME,
        installer="scripts/linux_privilege_separation.sh",
        status_command="sudo privacyfence-privilege-separation status",
        start_command="sudo systemctl restart privacyfence-daemon.service",
        stop_command="sudo systemctl stop privacyfence-daemon.service",
        enable_command="sudo privacyfence-privilege-separation enable",
        daemon_ctl_argv=(
            "systemctl", "show", "privacyfence-daemon.service",
            "-p", "ActiveState,SubState,Result,ExecMainStatus,MainPID",
        ),
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
        stop_command=f"sc.exe stop {WINDOWS_SERVICE_NAME}",
        enable_command=(
            'powershell -ExecutionPolicy Bypass -File '
            '"$env:ProgramFiles\\PrivacyFence\\privilege-separation.ps1" enable   (from an elevated PowerShell)'
        ),
        daemon_ctl_argv=("sc.exe", "query", WINDOWS_SERVICE_NAME),
    ),
}

SUPPORTED_PLATFORMS = tuple(PLATFORM_LAYOUTS)


class PrivilegeSeparationError(RuntimeError):
    """Raised by ``check_runtime_identity()`` when this install is separated
    but this process is not the account that is supposed to be holding the
    daemon's half of it -- see that function for why that has to be fatal
    rather than a warning."""


class SeparationHandover(PrivilegeSeparationError):
    """Not a failure: ADR 0003 decision 6's automatic ``enable`` just took,
    and the daemon this process was about to become now belongs to the
    service that ``enable`` created.

    A subclass rather than a return value because every caller of
    ``enforce_separation()`` already has to stop on
    ``PrivilegeSeparationError``, and stopping is exactly what this asks
    for; ``daemon_main.main()`` catches this one first and exits *0*,
    because nothing went wrong -- see that call site.

    Why this exists at all: before it, a packaged daemon that repaired its
    own install went on to serve from the layout it had just separated,
    **as the human**. ``check_runtime_identity()`` -- the gate that exists
    to stop precisely that -- had already run and passed, several steps
    earlier, when the install was still unseparated. So the process carried
    on into a root whose ``authority/`` is now ``0700`` to the service
    account, could not read ``config/settings.yaml`` through it, and
    ``load_config()``'s first-run behaviour seeded a fresh default policy
    over the real one: the silent policy reset ``check_runtime_identity()``
    is written to prevent, arrived at by a route it could not see. On
    Windows it also raced the service ``enable`` had just started for the
    same port and the same control pipe, which is the collision
    ``tests/integration/test_windows_packaged_smoke.py`` gave up its
    unseparated scenario over.
    """


@dataclass(frozen=True)
class Separation:
    """A parsed, validated marker file: what the installer provisioned."""

    version: int
    platform: str
    service_account: str
    service_group: str
    #: The human account this install was provisioned for, or ``""`` when
    #: ADR 0003 decision 3's machine half ran with nobody to add to the
    #: service group (an MDM push, an unattended ``apt`` upgrade, a ``.pkg``
    #: installed at the login window). Empty rather than absent on purpose:
    #: ``_parse_marker()`` below requires the key and refuses the whole
    #: marker without it, and a refused marker is a startup failure by
    #: design -- so ``""`` is the machine-readable spelling of "group
    #: membership pending", and ``is_enabled()`` stays true for it, because
    #: the install *is* separated. ``owner_membership_pending()`` is what
    #: reads it that way, and ``companion.py`` is what closes it.
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


def _default_system_root() -> Path | None:
    """``system_root()`` ignoring ``SYSTEM_ROOT_ENV_VAR`` entirely -- both
    its own fallback, and what it checks for a *real* marker before trusting
    the override at all (see B11 above)."""
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


def system_root() -> Path | None:
    """Where a separated install's root *would* be on this platform, whether
    or not one has been provisioned -- None on a platform #428 P4 hasn't
    shipped for yet, which is what makes every other function here a cheap
    no-op there."""
    default_root = _default_system_root()
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
        elif default_root is not None and _parse_marker(default_root / MARKER_FILE_NAME) is not None:
            # A real install is already provisioned at the platform's actual
            # root. Honouring the override now would let whatever set it --
            # on the companion or the MCPB shim, that's the user's own login
            # session -- redirect a process onto a root it controls instead
            # of the one the installer provisioned and locked down. That is
            # exactly what privilege separation exists to prevent, so this is
            # the one case the test/dev escape hatch does not get to bypass.
            logger.warning(
                "%s=%r ignored -- a real privilege-separation marker already exists at %s.",
                SYSTEM_ROOT_ENV_VAR, override, default_root,
            )
        else:
            return candidate
    return default_root


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
    """The mode ``paths.handoff_dir()`` is kept at -- ``3770`` when separated
    (setgid so the daemon and the companion keep producing group-owned files
    for each other regardless of which one creates them, plus the sticky bit
    the local-mode-fixes plan's interim multi-user guard adds -- see
    ``HANDOFF_DIR_MODE``'s own comment), and the ordinary ``0700`` otherwise,
    where ``handoff_dir()``
    *is* ``data_dir()`` and this must not change it."""
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


def service_account_uid() -> int | None:
    """The daemon's service-account uid on a separated POSIX install --
    None if this install isn't separated, if this platform has no uid
    concept at all (Windows: the boundary is an ACL, not a uid, see
    ``_current_user_security_attributes()`` in ``web/control_channel.py``),
    or if the account named in the marker doesn't exist locally (a
    half-removed install; the caller fails closed the same way
    ``_current_user_security_attributes()`` skips a missing Windows trustee
    rather than crashing).

    #428 B10's own reason to exist: ``web/control_channel.py``'s companion
    channel is the one place ``SO_PEERCRED``/``LOCAL_PEERCRED``'s uid is
    actually meaningful (ADR 0002 decision 6 is explicit that it is *not*,
    everywhere else, while the companion and the agent share a uid) --
    once separated, the daemon is the one end of that channel that has
    moved to a different account, so this is what a connecting peer's real
    uid gets checked against."""
    state = separation()
    if state is None or current_platform() == "win32":
        return None
    import pwd

    try:
        return pwd.getpwnam(state.service_account).pw_uid
    except KeyError:
        return None


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
    problems.extend(_posix_image_problems(daemon_image_paths()))
    return problems


def daemon_image_paths() -> tuple[Path, ...]:
    """The files this daemon's own frozen executable actually runs from --
    what a Windows service's ``binPath`` names (``windows_acl.
    image_problems()``), and, since B1, what a packaged macOS install's
    LaunchDaemon names (``_posix_image_problems()``).

    Two paths, not one, because there are two ways to replace what gets run:
    rewriting the executable itself, and dropping something next to it for
    the loader to find first (a DLL on Windows; nothing PyInstaller's
    one-dir macOS bundle actually loads that way today, but the parent
    directory is "write access to the install", so it's checked regardless).

    Empty on anything but a frozen build. A source or ``pip`` install's
    ``sys.executable`` is the Python interpreter, which is shared with every
    other Python program on the machine and is not something this install
    provisioned or can speak about -- reporting a user-writable
    ``python.exe``/``python3`` as a PrivacyFence layout defect would be
    noise on every developer's own machine, which is where unfrozen
    installs (including every current Linux ``.deb``) actually live.
    """
    if not (getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")):
        return ()
    image = Path(sys.executable)
    return (image, image.parent)


# root's own group on macOS and every BSD it descends from -- the one POSIX
# group this design extends the same trust to as it extends to root itself,
# for the same reason ``windows_acl.TRUSTED_TRUSTEES`` includes ``BUILTIN\\
# Administrators``. Deliberately *not* ``admin`` (the group ``/Applications``
# is actually group-owned by, and the one ADR 0002 §5a used to assume was
# safe the way ``/opt`` is): an ``admin`` member is any human with a local
# account who has ever answered a password prompt, which on a single-user
# Mac is the same account the agent runs as -- trusting it here would be
# trusting the exact account B1 exists to stop.
_TRUSTED_POSIX_IMAGE_GROUP = "wheel"


def _posix_image_paths_to_check(paths: tuple[Path, ...]) -> list[Path]:
    """``paths`` plus every directory on the way to each -- the "or any
    directory on the path to it" half of B1's fix, and the thing an NTFS ACL
    gets for free through inheritance that a POSIX mode bit does not: a
    root-owned, non-writable executable still isn't safe if the directory
    holding it can be emptied and refilled by someone else."""
    seen: dict[Path, None] = {}
    for path in paths:
        for candidate in (path, *path.parents):
            seen.setdefault(candidate)
    return list(seen)


def _posix_image_problems(paths: tuple[Path, ...]) -> list[str]:
    """The POSIX counterpart of ``windows_acl.image_problems()`` (B1) -- a
    ``stat`` walk rather than an ACL read, since POSIX has no inheritance to
    lean on. ``daemon_image_paths()`` is empty on every unfrozen install
    (source checkouts, the current Linux ``.deb``), so in practice this
    only ever finds something to say about a packaged macOS build.

    ADR 0002 §5a used to claim ``/Applications`` was root-owned the same way
    ``/opt`` is, which is false: it is ``root:admin drwxrwxr-x``, and a
    drag-installed ``.app`` is normally owned by the installing user -- the
    same account the agent runs as. So this checks the same thing Windows
    already refuses to run a service image on top of: is *anyone but root
    (or ``wheel``)* able to rewrite this, whether by owning it outright or
    through a writable group or world bit.

    Best-effort like every other check in this module: a path that can't be
    ``stat``'d is skipped rather than reported.
    """
    import grp
    import pwd

    try:
        trusted_gid = grp.getgrnam(_TRUSTED_POSIX_IMAGE_GROUP).gr_gid
    except KeyError:  # pragma: no cover -- no wheel group (non-BSD-derived POSIX)
        trusted_gid = 0

    problems: list[str] = []
    for candidate in _posix_image_paths_to_check(paths):
        try:
            st = candidate.stat()
        except OSError:
            continue
        if st.st_uid != 0:
            try:
                owner = pwd.getpwuid(st.st_uid).pw_name
            except KeyError:  # pragma: no cover -- a uid with no passwd entry
                owner = str(st.st_uid)
            problems.append(
                f"{candidate} is owned by '{owner}', not root -- the daemon runs whatever is at "
                "this path, so an owner other than root can replace it with anything and have "
                "that run as the service account."
            )
            continue
        mode = stat.S_IMODE(st.st_mode)
        if mode & stat.S_IWOTH:
            problems.append(
                f"{candidate} is world-writable (mode {mode:04o}) -- anyone on this machine can "
                "replace what the daemon runs."
            )
        elif mode & stat.S_IWGRP and st.st_gid != trusted_gid:
            try:
                group = grp.getgrgid(st.st_gid).gr_name
            except KeyError:  # pragma: no cover -- a gid with no group entry
                group = str(st.st_gid)
            problems.append(
                f"{candidate} is group-writable by '{group}' (mode {mode:04o}) -- only root and "
                f"'{_TRUSTED_POSIX_IMAGE_GROUP}' are trusted to replace what the daemon runs."
            )
    return problems


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
# macOS's own package-manager-equivalent hook is the .pkg's postinstall (#428
# D2), which the DMG now carries and which every ordinary macOS install goes
# through -- so this prompt is the fallback rather than the usual path: an
# install that never ran the installer (an app bundle copied off another
# machine, a source/pip run) still has nothing root-context behind it, and
# nothing short of a human answering an admin password prompt can create a
# system account or a LaunchDaemon. This is that prompt.
#
# ADR 0003 decision 6 removed the one-shot marker this used to write
# (``AUTO_ENABLE_ATTEMPTED_MARKER_NAME``): a decline used to be respected
# forever, silently. Under decision 6 a decline is not a configuration, it is
# an unfinished install, so this is asked again on every start that finds the
# install still unseparated -- and, on a packaged build, ``enforce_
# separation()`` below is what makes that matter: it is the thing that now
# calls this synchronously (never on a background thread the way the old,
# purely-cosmetic D1 prompt did) so it can act on the outcome rather than
# fire-and-forget it.


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


# Absolute path, alongside _OSASCRIPT below and for the same bandit B607
# reason: macOS ships this at a fixed location, so there is no PATH to
# resolve it against in the first place.
_CODESIGN = "/usr/bin/codesign"


def _posix_script_elevation_problem(script: Path) -> str | None:
    """The POSIX half of #428 B2's check (see
    ``_macos_auto_enable_script_problem()`` below for the whole argument):
    ``script`` is safe to run as root only if root is the one account that
    can rewrite it.

    Its own function because ADR 0003 decision 3 gave the module a second
    elevation -- the companion's ``enable --for-user`` -- which needs
    exactly this and, on Linux, nothing else: there is no bundle signature
    to check, and the script a Linux install elevates is the .deb's own
    root-owned ``/usr/sbin/privacyfence-privilege-separation``.
    """
    try:
        st = os.stat(script)
    except OSError as exc:
        return f"could not stat {script}: {exc}"
    if st.st_uid != 0:
        return f"{script} is owned by uid {st.st_uid}, not root"
    mode = stat.S_IMODE(st.st_mode)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{script} is group- or world-writable (mode {mode:04o})"
    return None


def _macos_auto_enable_script_problem(script: Path) -> str | None:
    """#428 B2: whether ``script`` is safe to run as root via the admin
    prompt ``maybe_auto_enable_macos()`` is about to trigger.

    Both places ``_macos_installer_script_path()`` can resolve to are, on
    their own, exactly as writable as any other file the logged-in user (and
    therefore the agent) owns: the .app bundle's ``Resources/`` is not
    special, and a source checkout is never root-owned. Running whatever is
    at that path as root on the strength of a routine-looking password
    dialog would hand an agent that can edit either of those a one-shot
    local privilege escalation, so this refuses unless the resolved script
    itself is owned by root and not group- or world-writable, and -- for a
    packaged install, where there is a signature to check at all -- unless
    the app bundle's signature still verifies. A source checkout can never
    satisfy the first of those, which is the point: it leaves auto-enable
    reachable only from a script the installer actually shipped, and every
    other case (including this one) falls back to the existing opt-in path,
    exactly as ``--auto`` already does whenever it cannot resolve something
    it needs.

    Returns ``None`` when ``script`` is safe to run, or a human-readable
    reason it is not. Best-effort like the rest of this module: a ``stat``
    or ``codesign`` failure reads as "not safe" rather than raising, since
    this sits directly in front of an elevation prompt and must never turn a
    transient error into one that runs anyway.
    """
    problem = _posix_script_elevation_problem(script)
    if problem is not None:
        return problem

    from . import paths

    bundle = paths.app_bundle_path()
    if bundle is None:
        return None
    try:
        result = subprocess.run(  # nosec B603  # fixed argv list below, no shell, nothing here is attacker-controlled
            [_CODESIGN, "--verify", "--deep", str(bundle)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not verify {bundle}'s signature: {exc}"
    if result.returncode != 0:
        return f"{bundle}'s signature does not verify: {result.stderr.strip()}"
    return None


def _applescript_quoted(text: str) -> str:
    """Escape ``text`` for a double-quoted AppleScript string literal
    (``\\`` and ``"`` are the only two characters that mean anything there).
    ``text`` here is already a full shell command built with
    ``shlex.quote()`` per argument -- that is the shell layer's own quoting,
    this is the AppleScript layer's on top of it, and neither substitutes
    for the other."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def maybe_auto_enable_macos() -> None:
    """#428 D1 (4.1) / ADR 0003 decision 6: on an unseparated macOS install,
    ask -- via the standard macOS admin-password dialog -- to run
    ``scripts/macos_privilege_separation.sh enable`` on this human's behalf.

    Called from ``enforce_separation()`` below, synchronously: decision 6's
    gate has to know whether this took before it decides to refuse to serve,
    which a fire-and-forget background thread cannot answer. A no-op on
    every other platform, on an already-separated install, and when the
    script this needs isn't packaged into the running app (a test process,
    or a source checkout run without ``scripts/`` next to it). Also a no-op
    -- logged, not raised -- when ``_macos_auto_enable_script_problem()``
    finds the resolved script is not something this prompt should run as
    root; see that function for why (#428 B2). That check never passes for a
    source checkout, which is deliberate: this prompt only ever runs a
    script the installer itself shipped.

    Asked again on every start that finds the install still unseparated --
    decision 6 retired the one-shot ``AUTO_ENABLE_ATTEMPTED_MARKER_NAME``
    marker this used to write, because under this ADR a decline is not a
    configuration, it is an unfinished install.

    Every failure is non-fatal and logged rather than raised: the caller,
    ``enforce_separation()``, is what turns "still unseparated after this"
    into a refusal -- this function only ever attempts.
    """
    if current_platform() != "darwin":
        return
    if is_enabled():
        return
    script = _macos_installer_script_path()
    if script is None:
        return
    problem = _macos_auto_enable_script_problem(script)
    if problem is not None:
        logger.warning("skipping automatic privilege-separation enable: %s", problem)
        return
    _run_auto_enable_macos(script)



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
    logger.info("privilege separation enabled automatically (#428 D1 / ADR 0003 decision 6)")


# ── ADR 0003 decision 6: a packaged daemon that finds itself unseparated ────
# does not serve ──────────────────────────────────────────────────────────────
#
# Decisions 2-5 cover the installs we ship: a .pkg, a Windows installer run
# and a .deb postinst that each separate at install time. This is the
# backstop for the installs that exist anyway -- a pre-4.2 DMG install
# upgrading in place, a restored backup, a hand-copied .app, an install where
# `disable` was run and forgotten. Called from daemon_main.main(), after
# check_runtime_identity() and before the daemon opens /mcp or the approvals
# UI to anything.
#
# Deliberately scoped to `paths.is_bundled()` -- a *packaged* build -- and
# nothing else. That is also, by construction, "local mode and nothing else":
# every packaged PrivacyFence build is one of the three desktop installers
# this ADR's decisions 2/4/5 cover, and org mode is never shipped that way
# (ADR 0002's "Out of scope" -- its daemon runs somewhere the agent has no
# access at all, deployed from the wheel/sdist, see decision 7). A source
# checkout or a `pip install` -- whether that is local-mode development or a
# real org-mode deployment -- is unaffected here; decision 7's "say what they
# are" obligation is met instead by step_up_config.py refusing
# `require_passkey` on an unseparated local-mode install (the one place a
# non-packaged build could otherwise claim a guarantee it does not hold).
DEV_ALLOW_UNSEPARATED_ENV = "PRIVACYFENCE_DEV_ALLOW_UNSEPARATED"


def dev_allows_unseparated() -> bool:
    """The escape hatch decision 7 names for the developer path -- "a
    sibling of ``PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP``" (org_identity.py's
    ``_dev_allows_insecure_idp()``, same spelling, same reasoning: never set
    in a real deployment). Consulted by ``step_up_config.py`` when a
    non-packaged install's ``config/settings.yaml`` asks for
    ``require_passkey`` without being separated -- see that module's
    ``from_local_config()``. ``enforce_separation()`` below does *not*
    consult this: decision 6's refusal is unconditional on a packaged build,
    with no developer override, because that is the one case where "an
    install lying about its own guarantee" is a real shipped product rather
    than a checkout somebody is actively working on."""
    return os.environ.get(DEV_ALLOW_UNSEPARATED_ENV, "") not in ("", "0", "false", "False")


@contextlib.contextmanager
def _elevation_transcript() -> Iterator[Path]:
    """A throwaway file for an elevated child's own output, and the reason
    there has to be a file at all.

    ``subprocess``'s ``capture_output`` catches what the process this module
    starts writes -- and on Windows that process is only the launcher. A
    ``Start-Process -Verb RunAs`` child is started by the shell, into a
    console of its own, so neither its stdout nor its stderr is inherited
    from anything the daemon can read. The only channel left is a file both
    sides can name, which is what this is: a directory in the daemon's own
    temp, removed again as soon as the output has been read back.

    It is created by this (unelevated) process on purpose. A file an
    elevated child creates under ``%TEMP%`` inherits that directory's ACL
    and would normally be readable anyway, but "normally" is doing real work
    in that sentence -- creating it here means the daemon's access to it
    never depends on how the elevation resolved.
    """
    directory = tempfile.mkdtemp(prefix="privacyfence-elevate-")
    try:
        transcript = Path(directory) / "privilege-separation.log"
        transcript.touch()
        yield transcript
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _elevation_transcript_text(transcript: Path | None, *, max_chars: int = 4000) -> str:
    """What the elevated child said, trimmed to something a log line can
    carry, or ``""`` where it said nothing this process can read.

    Best-effort by construction: this runs on the failure path of an
    elevation that has already gone wrong, and a transcript that cannot be
    read is one more thing to report rather than a reason to raise.
    """
    if transcript is None:
        return ""
    try:
        # -Encoding utf8 on the writing side (see
        # _windows_elevated_script_command); utf-8-sig because Windows
        # PowerShell 5.1's "utf8" means "UTF-8 with a BOM", and the catch
        # branch's -Append can put a second one mid-file.
        text = transcript.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    text = text.replace("\ufeff", "").strip()
    if len(text) > max_chars:
        text = "..." + text[-max_chars:]
    return text


def _elevation_detail(result: "subprocess.CompletedProcess[str]", transcript: Path | None) -> str:
    """One string naming everything known about how an elevation went: the
    launcher's own stderr, the elevated child's transcript, and -- when
    neither said anything -- the exit code, so the log line is never empty.
    """
    parts = [part for part in ((result.stderr or "").strip(), _elevation_transcript_text(transcript)) if part]
    return " | ".join(parts) if parts else f"no output (exit {result.returncode})"


def _windows_elevated_script_command(script: Path, script_arguments: str, transcript: Path) -> str:
    """The elevated PowerShell's own ``-Command``: run ``script`` with
    ``script_arguments``, with all six of its output streams merged into
    ``transcript``, and exit with the script's own exit code.

    ``-Command`` rather than the ``-File`` this used to pass, for the one
    thing ``-File`` cannot buy: a ``-Verb RunAs`` child gets a console of
    its own, and ``Start-Process``'s ``-RedirectStandardOutput``/
    ``-RedirectStandardError`` are in a different parameter set from
    ``-Verb`` and cannot be combined with it. The redirection therefore has
    to be established *inside* the elevated process, which means it has to
    be part of the command that process runs. ``& '<script>'`` is ``-File``
    spelled as an expression, and ``_powershell_quoted`` keeps the guarantee
    the ``-File`` form needed (see ``_windows_runas_argv``): a path with a
    space in it stays one argument.

    Three details that are each load-bearing:

    * ``*>&1 | Out-File -Encoding utf8`` rather than a bare ``*>``. Windows
      PowerShell 5.1 writes a plain ``>``/``*>`` redirection as UTF-16, which
      the reader above would have to sniff; naming the encoding means it
      does not have to.
    * The ``catch``. ``Stop-WithError`` in the script is a ``throw``, which
      unwinds *past* the redirection rather than through it -- so without
      this, the one message that says why an ``enable`` failed would be the
      one message missing from the transcript.
    * ``exit $LASTEXITCODE``. A ``.ps1`` invoked with ``&`` sets it from its
      own ``exit``, but leaves it untouched if it falls off the end, so the
      null case is spelled out rather than left to coerce to 0 by accident.

    ``script_arguments`` arrives already spelled as PowerShell rather than
    as a list, for the same reason ``Start-Process`` below is handed a
    pre-built command line: the quoting is not uniform and cannot be applied
    by a rule. PowerShell binds ``-ForUser`` as a parameter *name* only
    while it is a bare token -- quote it and ``enable -ForUser alice``
    reaches the script as three positional arguments that bind nothing --
    while the value beside it is an account name and must be quoted. Each
    caller therefore composes its own, through ``_powershell_quoted`` for
    every part that is data.
    """
    quoted = _powershell_quoted(str(transcript))
    call = f"& {_powershell_quoted(str(script))} {script_arguments}"
    return (
        f"try {{ {call} *>&1 | Out-File -LiteralPath {quoted} -Encoding utf8 }} "
        f"catch {{ $_ | Out-String | Out-File -LiteralPath {quoted} -Append -Encoding utf8; exit 1 }}; "
        "if ($null -eq $LASTEXITCODE) { exit 0 }; exit $LASTEXITCODE"
    )


def _windows_runas_argv(script: Path, script_arguments: str, transcript: Path) -> list[str]:
    """The elevated relaunch both Windows callers below share: an ordinary
    PowerShell whose only job is to ``Start-Process -Verb RunAs`` (UAC) a
    second PowerShell running ``script script_arguments``, and to end with
    that second PowerShell's exit code.

    ``-PassThru`` and ``exit $p.ExitCode`` are the whole of privacyfence/
    privacyfence#599's first half, and they are not a refinement of
    ``-Wait``. ``-Wait`` waits, and that is all it does: without
    ``-PassThru`` there is no process object to read a code off, so the
    launcher exits 0 whether the elevated ``enable`` completed or died on
    its first statement. Every caller below then took its success branch on
    a number that carried no information -- the daemon logged "privilege
    separation enabled automatically" at the exact moment separation had not
    happened, on a machine where it never once did.

    ``-ArgumentList`` is handed *one* pre-built command line rather than the
    obvious array of arguments, and that is the other reason this function
    exists. ``Start-Process`` joins an ``-ArgumentList`` array with plain
    spaces and quotes nothing, so
    ``"-File", r"C:\\Program Files\\PrivacyFence\\privilege-separation.ps1"``
    reached the elevated process as ``-File C:\\Program Files\\...``: PowerShell
    read ``C:\\Program`` as the script to run, could not find it, and exited
    nonzero. On the default install location -- the only one most people
    have -- that made every automatic ``enable`` fail *after* its UAC prompt
    had already been approved, and since the daemon's own autostart task
    repeats every five minutes, a prompt that could never accomplish
    anything came back every five minutes for as long as the install stayed
    unseparated.

    ``subprocess.list2cmdline`` builds that command line by the rules
    ``CommandLineToArgvW`` parses it back out with, which is how the
    elevated PowerShell reads its own arguments; ``_powershell_quoted``
    then carries the result through the outer ``-Command`` as a single
    literal.

    The ``$null -eq $p`` guard is the declined-UAC path. ``Start-Process
    -Verb RunAs`` raises when consent is refused, which at PowerShell's
    default ``$ErrorActionPreference`` is written as an error record and
    execution continues -- leaving ``$p`` unset. Without the guard the next
    line would read ``.ExitCode`` off nothing and the launcher would exit 0
    again, which is the same lie this function exists to stop telling.
    """
    powershell = str(_windows_system32("WindowsPowerShell\\v1.0\\powershell.exe"))
    inner = [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        _windows_elevated_script_command(script, script_arguments, transcript),
    ]
    return [
        powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        f"$p = Start-Process -FilePath {_powershell_quoted(powershell)} -Verb RunAs -PassThru -Wait "
        f"-ArgumentList {_powershell_quoted(subprocess.list2cmdline(inner))}; "
        "if ($null -eq $p) { exit 1 }; exit $p.ExitCode",
    ]


def _windows_full_enable_argv(script: Path, transcript: Path) -> list[str]:
    """The elevated invocation of the *whole* ``enable`` (both halves) on
    Windows -- ``enforce_separation()``'s own attempt, run when a packaged
    build finds itself unseparated at all.

    No ``-Auto`` here: unlike the POSIX scripts, ``windows_privilege_
    separation.ps1`` has no such flag, and needs none -- UAC's elevation
    prompt already runs as a real interactive session (unlike ``pkexec``, it
    is never headless), so ``enable`` alone already resolves the current
    user as the owner and completes both halves in one call with a real
    (non-``--auto``) exit code. See ``PlatformLayout.enable_command`` for
    the same command spelled out for a human to type by hand.
    """
    return _windows_runas_argv(script, "enable", transcript)


def _log_handlers_under(directory: Path) -> list[logging.FileHandler]:
    """Every ``FileHandler`` this process has attached whose file lives
    inside ``directory`` -- root logger first, then any logger that
    configured its own.

    ``daemon_main.setup_logging()`` puts both handlers on the root logger
    and everything else inherits them, so in the shipped daemon this finds
    exactly one. It walks the rest anyway because the cost is a dictionary
    scan and the failure it guards against is invisible until a release
    build hits it on somebody's machine.
    """
    loggers: list[logging.Logger] = [logging.getLogger()]
    loggers.extend(
        obj for obj in logging.Logger.manager.loggerDict.values()
        if isinstance(obj, logging.Logger)
    )
    found: list[logging.FileHandler] = []
    for log in loggers:
        for handler in list(log.handlers):
            if not isinstance(handler, logging.FileHandler) or handler in found:
                continue
            try:
                base = Path(handler.baseFilename)
            except (AttributeError, TypeError, ValueError):  # pragma: no cover -- defensive
                continue
            if base.is_relative_to(directory):
                found.append(handler)
    return found


@contextlib.contextmanager
def _data_dir_log_files_released() -> Iterator[None]:
    """Close the log files this process holds open inside the data directory
    the elevated ``enable`` is about to **move**, and let them reopen
    afterwards.

    The third sighting of one defect, and the first where the process
    holding the file is the one that asked for the move. ``disable`` moved
    ``%ProgramData%\\PrivacyFence`` out from under a service it had only
    *asked* to stop (fixed in 1d6b13f) and out from under a companion whose
    task it had deleted without ending the process; ``enable`` moves
    ``%LOCALAPPDATA%\\PrivacyFence`` out from under **its own caller** --
    ``enforce_separation()`` runs from inside a packaged daemon that has
    already called ``daemon_main.setup_logging()``, so
    ``logs/privacyfence.log`` is open for append in this very process for
    the whole elevated run. Windows has no POSIX rename-over-open-files
    escape hatch, so that is a sharing violation every time::

        -> moving C:\\Users\\...\\AppData\\Local\\PrivacyFence to C:\\ProgramData\\PrivacyFence
        Move-Item : The process cannot access the file because it is being used by another process.
            + CategoryInfo : WriteError: (privacyfence.log:FileInfo) [Move-Item], IOException

    -- after which ``Undo-PartialEnable`` rolls the whole thing back and
    decision 6 refuses to serve, which is how a Scheduler-started daemon
    on a hosted runner (and on any machine whose install is not separated
    yet) produced no control pipe at all.

    Nothing is lost while the window is open: ``FileHandler.emit()``
    reopens ``baseFilename`` by itself whenever ``stream`` is ``None`` --
    that is how ``delay=True`` works -- so a record logged in the middle of
    the elevated run simply opens the file again. That is a real hole, and
    it is why this wraps the ``subprocess.run`` call alone rather than the
    whole attempt: no ``logger`` call of ours is inside it.

    Afterwards the handlers are pointed at wherever the data directory now
    is. On the failure path that is the same file they had; on the success
    path it is the separated root, which this process usually cannot write
    -- so a handler that cannot be re-established is detached instead, and
    the last thing this process has to say reaches stderr rather than
    raising out of a log call.
    """
    from . import paths  # local, like every other paths import here -- paths imports this module

    before = paths.data_dir()
    handlers = _log_handlers_under(before)
    relative: dict[logging.FileHandler, Path] = {}
    for handler in handlers:
        relative[handler] = Path(handler.baseFilename).relative_to(before)
        handler.acquire()
        try:
            stream = handler.stream
            handler.flush()
            handler.stream = None  # type: ignore[assignment]  # reopened lazily by emit()
        finally:
            handler.release()
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
    try:
        yield
    finally:
        reset_cache()
        after = paths.data_dir()
        for handler in handlers:
            target = after / relative[handler]
            try:
                os.makedirs(target.parent, exist_ok=True)
                # Proved, not assumed: on the success path this is the
                # separated root, whose ACLs name the service account and
                # Administrators -- and `emit()` finding that out for itself
                # turns every later log call into a handleError() traceback
                # on a stderr a windowed build does not have.
                with open(target, "a", encoding="utf-8"):
                    pass
            except OSError:
                _detach_handler(handler)
                continue
            handler.baseFilename = str(target)


def _detach_handler(handler: logging.Handler) -> None:
    """Remove one handler from every logger holding it -- the fallback when
    the file it wrote to has moved somewhere this process may not write.

    A detached handler is strictly better than one that raises out of every
    subsequent ``logger.info()``: the stderr ``StreamHandler``
    ``setup_logging()`` installs alongside it is still attached, so the
    handover message below still has somewhere to go.
    """
    logging.getLogger().removeHandler(handler)
    for obj in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(obj, logging.Logger):
            obj.removeHandler(handler)


def _run_full_auto_enable_non_macos() -> None:
    """The Windows/Linux siblings of ``maybe_auto_enable_macos()`` --
    dispatched from ``enforce_separation()`` only, never from a install path
    that already knows its own owner (the Windows installer and the .deb's
    postinst both call the provisioning script directly, elevated by their
    own install-time context; this is specifically the backstop for an
    install that has neither).

    Best-effort and non-fatal like every other elevation attempt in this
    module: every failure is logged, and the caller's own re-check of
    ``is_enabled()`` -- not this function's return -- is what decides
    whether to refuse.

    It re-checks ``is_enabled()`` for its *own* log line too, which is the
    second half of privacyfence/privacyfence#599 and is deliberately
    belt-and-braces with the exit-code plumbing in
    ``_windows_runas_argv()``. A success message here is the only account a
    user or an operator gets of what happened during a start that then
    refused to serve, and it was being written off a subprocess return code
    rather than off the machine. Nothing about an elevation's exit code is
    trustworthy enough to keep saying "separation enabled" without looking,
    so this looks.
    """
    platform = current_platform()
    script = installer_script_path()
    if script is None:
        logger.warning("no privilege-separation provisioning script found for %s -- nothing to run", platform)
        return
    problem = _elevation_script_problem(script)
    if problem is not None:
        logger.warning("skipping automatic privilege-separation enable: %s", problem)
        return
    with contextlib.ExitStack() as stack:
        transcript: Path | None = None
        if platform == "win32":
            transcript = stack.enter_context(_elevation_transcript())
            argv = _windows_full_enable_argv(script, transcript)
        else:
            pkexec = shutil.which("pkexec")
            if pkexec is None:
                logger.warning(
                    "no pkexec on this system -- cannot prompt for a password to enable privilege "
                    "separation automatically. Run: sudo %s enable", script,
                )
                return
            argv = [pkexec, str(script), "enable", "--auto"]
        try:
            # The elevated run *moves* this process's own data directory, so
            # nothing of ours may be holding a file in it while it does --
            # see _data_dir_log_files_released(), and note that it wraps
            # this call alone rather than the function, because a logger
            # call inside the window would simply reopen the file.
            with _data_dir_log_files_released():
                result = subprocess.run(  # nosec B603  # fixed argv built above, no shell, quoted per layer
                    argv, capture_output=True, text=True, timeout=300, check=False,
                )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("automatic privilege-separation enable did not run", exc_info=True)
            return
        # Inside the ExitStack: the transcript is removed on the way out, so
        # it has to be read while it is still there.
        detail = _elevation_detail(result, transcript)
    if result.returncode != 0:
        # A declined UAC/polkit prompt lands here and is an expected outcome,
        # not a bug -- the same reading `_run_auto_enable_macos()` takes of
        # osascript's own nonzero exit.
        logger.info("automatic privilege-separation enable did not complete: %s", detail)
        return
    reset_cache()
    if not is_enabled():
        logger.error(
            "automatic privilege-separation enable exited 0 but this install is still not "
            "separated -- the elevated run got part of the way and stopped. What it said: %s",
            detail,
        )
        return
    logger.info("privilege separation enabled automatically (ADR 0003 decision 6)")


def enforce_separation() -> None:
    """ADR 0003 decision 6: a packaged local-mode daemon that finds itself
    unseparated does not serve.

    A no-op on anything but a packaged build (``paths.is_bundled()`` --
    ``sys.frozen``/``_MEIPASS``, see this module's own docstring on why that
    is, in practice, "local mode and nothing else") and on an
    already-separated install. Otherwise it attempts this platform's
    provisioning -- ``maybe_auto_enable_macos()`` on macOS, the Windows/Linux
    equivalent above everywhere else -- and then raises, whichever way that
    went, because *neither* outcome leaves this process a daemon to be:

    * still unseparated: ``PrivilegeSeparationError`` naming the one command
      that fixes it, the same fail-closed posture ``check_runtime_identity()``
      already takes and for the same reason -- the alternative failure is
      silent, and it does not degrade the product visibly, it invalidates a
      guarantee the UI is still making.
    * separated by the attempt: ``SeparationHandover``, which is not a
      failure and which ``daemon_main.main()`` exits 0 on. The install now
      has a service running the daemon under an account this process is
      not, and the gate that would ordinarily say so ran several steps
      earlier, when there was nothing to say. See ``SeparationHandover``.

    So this returns only when it did nothing.

    No developer override here -- see ``dev_allows_unseparated()``'s own
    docstring for why decision 6's refusal is unconditional on a packaged
    build.
    """
    from . import paths

    if not paths.is_bundled():
        return
    if is_enabled():
        return
    layout = platform_layout()
    if layout is None:  # pragma: no cover -- SUPPORTED_PLATFORMS gates every packaged build
        return
    if current_platform() == "darwin":
        maybe_auto_enable_macos()
    else:
        _run_full_auto_enable_non_macos()
    reset_cache()
    if is_enabled():
        # It took -- and that settles what this process is, not just what
        # the install is. `enable` has just handed the daemon to a service
        # account this process is not, and the identity gate that would
        # have said so (check_runtime_identity(), daemon_main.main()'s
        # second call) ran while the install was still unseparated. Stop
        # here instead of serving the layout we just separated; see
        # SeparationHandover.
        raise SeparationHandover(
            "PrivacyFence has just separated this install (ADR 0003 decision 6). The daemon now "
            f"runs as '{layout.service_account}', started by the system rather than by this "
            "session, so this process -- which was started before that happened, as "
            f"'{current_user_name()}' -- is stopping. Nothing went wrong and nothing else needs "
            f"doing; run '{layout.status_command}' to see the install."
        )
    raise PrivilegeSeparationError(
        "This is a packaged PrivacyFence install, and it is not privilege-separated (#428 Phase "
        "4 / ADR 0003 decision 6). The agent and the daemon would run under the same account, so "
        "a local process could mint its own session and approve its own request -- the guarantee "
        "the approvals UI claims would not actually hold. Refusing to start: no /mcp, no "
        f"approvals. Run '{layout.enable_command}' to fix this, then restart PrivacyFence."
    )


def dev_unseparated_notice() -> str | None:
    """ADR 0003 decision 7's disclosure: ``None`` unless this is a
    non-packaged build running unseparated *with* the developer override set
    (``dev_allows_unseparated()``) -- the one case that is actually running,
    not refused, while still not privilege-separated. ``None`` on a
    packaged build (which either separated or already refused to start --
    ``enforce_separation()`` above), on an already-separated install, and on
    a non-packaged build that hasn't set the override (nothing here claims
    protection it doesn't have, so there's nothing to disclose beyond the
    ordinary "step-up isn't on" notice ``step_up_config.off_notice()``
    already gives).

    Consulted from two places so neither can drift from the other: daemon_
    main.py logs this at startup, and web/routes_security.py's local-mode
    /security page shows it -- "says on /security and in the startup log
    that this install's approvals are not protected against the client
    they govern" (ADR 0003 decision 7's own wording).
    """
    from . import paths

    if paths.is_bundled():
        return None
    if is_enabled():
        return None
    if not dev_allows_unseparated():
        return None
    return (
        f"{DEV_ALLOW_UNSEPARATED_ENV} is set on this non-packaged install, which is not "
        "privilege-separated -- approvals are NOT protected against the AI client they govern. "
        "Never set this in a real deployment (ADR 0003 decision 7)."
    )


# ── ADR 0003 decision 3: the per-user half ───────────────────────────────────
#
# ``enable``'s machine half provisions everything root can do alone and records
# ``owner_user: ""`` -- the marker's spelling of "group membership pending"
# (see ``Separation.owner_user``). What is left is one re-runnable step per
# human: adding them to the service group, and moving whatever they had under
# ~/.privacyfence before the install. That step is exactly what the companion
# app is in a position to take -- it is the one PrivacyFence process that runs
# inside a real login session, as the person whose membership is missing -- so
# this is the mechanism it drives. See ``companion.py``'s own call site for
# when.


def _parse_net_localgroup(output: str) -> frozenset[str]:
    """The member names out of ``net localgroup <name>``'s report.

    ``net.exe`` prints a header, a row of dashes, one member per line and a
    localized "the command completed successfully" footer, so everything
    after the dashes is a member except that last line -- which is left in
    rather than matched against a localized string. It is harmless: the only
    thing this set is ever asked is whether a *particular* account is in it,
    and the answer for an account named after a sentence of English prose is
    wrong in the safe direction (no prompt).
    """
    members: set[str] = set()
    after_separator = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and set(stripped) == {"-"}:
            after_separator = True
            continue
        if after_separator and stripped:
            # ``DOMAIN\alice`` and ``alice`` are the two spellings net.exe
            # uses; ``current_user_name()`` answers with the account half.
            members.add(stripped.rsplit("\\", 1)[-1])
    return frozenset(members)


def _windows_system32(name: str) -> Path:
    """An absolute path to one of Windows' own tools, for the same bandit
    B607 reason ``_OSASCRIPT`` is absolute: a partial executable name is
    resolved against a PATH this process does not control."""
    return Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / name


def _windows_local_group_members(group: str) -> frozenset[str] | None:
    """``group``'s recorded members, read with ``net localgroup``.

    Not pywin32: ``windows_acl`` needs it for security descriptors and this
    does not, and a build without it still has to be able to answer this --
    the alternative is a companion that prompts for a password at every
    start on exactly the installs where it cannot check first.
    """
    try:
        result = subprocess.run(  # nosec B603  # fixed argv, no shell; `group` is a module constant
            [str(_windows_system32("net.exe")), "localgroup", group],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("Could not read the %s local group", group, exc_info=True)
        return None
    if result.returncode != 0:
        return None
    return _parse_net_localgroup(result.stdout)


def service_group_members(group: str) -> frozenset[str] | None:
    """Who is *recorded* as a member of ``group``, or None where that cannot
    be read at all.

    Recorded rather than live on purpose. Group membership is evaluated when
    a session is created on all three platforms, so a session that predates
    ``enable`` does not carry it and never will until the human logs out and
    back in -- reading this process's own token (``os.getgroups()``,
    ``whoami /groups``) would therefore have the companion re-run an
    elevated command at every start until they did, prompting for a password
    to fix something no password can fix.

    Only supplementary membership is read, because that is the only kind the
    installers ever grant: ``usermod -aG``, ``dseditgroup -o edit -a`` and
    ``Add-LocalGroupMember`` all add a human to a system group that is
    nobody's primary.
    """
    if current_platform() == "win32":
        return _windows_local_group_members(group)
    import grp

    try:
        return frozenset(grp.getgrnam(group).gr_mem)
    except KeyError:
        # No such group: the machine half creates it, so this is an install
        # whose marker outlived its provisioning (a restored backup, a
        # half-removed install). Not something a per-user re-run can fix.
        return None


def owner_membership_pending() -> bool:
    """Whether this install is separated and this process's account is still
    outside the service group -- decision 3's "pending" state.

    False on an unseparated install: there is no group to be outside of, and
    an install with no separation at all is a different problem with a
    different answer (ADR 0003 decision 6's daemon-side gate).

    Through the local-mode-fixes plan's Phase 2 (§2.6), this was also false
    for any account that was not this install's recorded owner -- a second
    OS user was refused rather than onboarded, to close a leak (a shared
    principal, a takeable companion socket) that ADR 0008's Phase 3 has since
    fixed at its actual source. Now that a second account gets its own
    isolated principal (``os-<uid>``/``os-<sid>``) instead of the owner's,
    there is nothing left for an owner-mismatch to protect against here: any
    service-group member who has not yet joined is "pending" in exactly the
    sense the owner always was, and ``companion.py``'s own
    ``_complete_pending_separation()`` completing ``enable --for-user`` on
    their behalf hands them their own empty principal, never the owner's.
    See ADR 0008 ("D2: two identities, not one, per install") for the full
    account of what changed and why.
    """
    state = separation()
    if state is None:
        return False
    members = service_group_members(state.service_group)
    if members is None:
        # The group could not be read. The marker is the one thing every
        # platform writes the same way, so fall back to it: an empty
        # owner_user is pending by construction, and a filled-in one means
        # *some* human was added -- guessing it was not this one, on a
        # platform we just failed to interrogate, would prompt at every start.
        return not state.owner_user
    user = current_user_name()
    return not any(accounts_equal(member, user) for member in members)


def owner_uid() -> int | None:
    """The uid of this install's recorded owner (``Separation.owner_user``),
    or ``None`` if there is none recorded yet, this platform has no uid
    concept (Windows -- see ``owner_sid()``), or the named account does not
    exist locally (a marker surviving the account's own removal). ADR 0008's
    own principal-id mapping: a control-channel peer whose uid equals this
    one maps to ``LOCAL_PRINCIPAL`` rather than to ``os-<uid>``, so the
    owner's own existing data stays exactly where it already is. Mirrors
    ``service_account_uid()``'s own shape, resolving the marker's
    ``owner_user`` name instead of its ``service_account`` name."""
    state = separation()
    if state is None or not state.owner_user or current_platform() == "win32":
        return None
    import pwd

    try:
        return pwd.getpwnam(state.owner_user).pw_uid
    except KeyError:
        return None


def owner_sid() -> str | None:
    """Windows' equivalent of ``owner_uid()``: the recorded owner's SID as a
    string (``S-1-5-21-...``), or ``None`` under the same conditions
    ``owner_uid()`` returns ``None`` for on POSIX. Used the same way: a
    control-channel peer whose SID matches this one is ``LOCAL_PRINCIPAL``,
    not ``os-<sid>``."""
    state = separation()
    if state is None or not state.owner_user or current_platform() != "win32":
        return None
    return _resolve_owner_sid(state.owner_user)


def _resolve_owner_sid(owner_user: str) -> str | None:  # pragma: no cover -- exercised by the platform-windows job
    from . import windows_acl

    sid = windows_acl.lookup_account_sid(owner_user)
    if sid is None:
        return None
    import win32security

    return win32security.ConvertSidToStringSid(sid)


#: Where ``scripts/build_deb.sh`` installs the Linux provisioning script, and
#: the name ``PLATFORM_LAYOUTS["linux"].status_command`` quotes. Its own
#: constant rather than a literal inside ``installer_script_path()`` so a test
#: can point it somewhere that exists -- on a runner of any OS, which a
#: hardcoded POSIX path cannot be.
LINUX_PACKAGED_INSTALLER = Path("/usr/sbin/privacyfence-privilege-separation")


def _checkout_script_path(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / name


def _first_existing(*candidates: Path) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def installer_script_path() -> Path | None:
    """This platform's provisioning script, for *this* running process --
    the packaged copy each installer lays down, or the repo-relative one a
    source checkout has. None when neither is there.

    The packaged copy is the one that matters: ``_elevation_script_problem()``
    refuses a checkout below, deliberately, so that the only thing this
    module ever runs as root is a script an installer shipped.
    """
    platform = current_platform()
    if platform == "darwin":
        return _macos_installer_script_path()
    if platform == "linux":
        return _first_existing(
            LINUX_PACKAGED_INSTALLER,
            _checkout_script_path("linux_privilege_separation.sh"),
        )
    if platform == "win32":
        # ``installer/privacyfence.iss`` renames it on the way into {app},
        # which is the directory a frozen build's own executable sits in.
        return _first_existing(
            Path(sys.executable).parent / "privilege-separation.ps1",
            _checkout_script_path("windows_privilege_separation.ps1"),
        )
    return None  # pragma: no cover -- SUPPORTED_PLATFORMS gates every caller


def _windows_script_elevation_problem(script: Path) -> str | None:
    """#428 B2's check, in the one idiom Windows has for it. There is no uid
    to compare, so the question is the one ``windows_acl.image_problems()``
    asks of the daemon's own image and for the same reason: whether anything
    outside SYSTEM and Administrators can rewrite what is about to run
    elevated."""
    from . import windows_acl

    aces = windows_acl.read_dacl(script)
    if aces is None:
        return f"could not read {script}'s ACL"
    writable = sorted(
        {ace.trustee for ace in aces if ace.grants_write() and not windows_acl.is_trusted(ace.trustee)}
    )
    if writable:
        return f"{script} is writable by {', '.join(writable)}"
    return None


def _elevation_script_problem(script: Path) -> str | None:
    platform = current_platform()
    if platform == "darwin":
        return _macos_auto_enable_script_problem(script)
    if platform == "win32":
        return _windows_script_elevation_problem(script)
    return _posix_script_elevation_problem(script)


def _powershell_quoted(text: str) -> str:
    """Escape ``text`` for a single-quoted PowerShell string literal, where
    doubling the quote is the whole of the escaping rule and nothing else --
    backslashes included -- means anything."""
    return "'" + text.replace("'", "''") + "'"


def per_user_command_text(script: Path, user: str) -> str:
    """The one command a human would type to do this by hand -- what gets
    logged wherever this module cannot ask for a password itself."""
    if current_platform() == "win32":
        return (
            f'powershell -ExecutionPolicy Bypass -File "{script}" enable -ForUser {user}'
            "   (from an elevated PowerShell)"
        )
    return f"sudo {shlex.quote(str(script))} enable --for-user {shlex.quote(user)}"


def _macos_admin_argv(shell_command: str, *, prompt: str | None = None) -> list[str]:
    """The ``osascript ... with administrator privileges`` argv both macOS
    elevations share (this module's own ``enable --for-user``, and
    ``service_control.py``'s ``daemon start/stop/restart``): one system
    dialog, run against ``shell_command`` exactly as ``shlex.quote()`` built
    it -- this function adds only AppleScript's own string-literal escaping
    on top, never re-quotes the command itself.

    ``prompt`` is the dialog's explanatory line. Omitted by default (a bare
    ``with administrator privileges``, ``_per_user_argv()``'s own long-
    standing text) because that call already explains itself through
    ``docs/platform-support.md``'s "group membership" flow; a caller putting
    a *new* password dialog in front of somebody (a tray click, not a
    startup check) should pass one, so the system prompt says why it
    appeared instead of asking cold.
    """
    prompt_clause = f" with prompt {_applescript_quoted(prompt)}" if prompt else ""
    return [
        _OSASCRIPT, "-e",
        f"do shell script {_applescript_quoted(shell_command)} with administrator privileges{prompt_clause}",
    ]


def _pkexec_argv(script: Path, *args: str) -> list[str] | None:
    """``pkexec script *args``, or None where this desktop has no ``pkexec``
    at all -- the Linux elevation both this module's own ``enable
    --for-user`` and ``service_control.py``'s ``daemon start/stop/restart``
    share. None is the reason this returns rather than guessing: a bare
    ``sudo`` with no askpass in a systemd-started process hangs on a
    password prompt nobody can see, so every caller here treats a missing
    ``pkexec`` as "cannot elevate from here", not as a fallback to try."""
    pkexec = shutil.which("pkexec")
    if pkexec is None:
        return None
    return [pkexec, str(script), *args]


def _per_user_argv(script: Path, user: str, transcript: Path) -> list[str] | None:
    """The elevated invocation of ``enable --for-user``, or None where this
    platform has no way to ask for the password from a login session.

    ``transcript`` is where the elevated child's output is to be collected;
    only the Windows branch has anything to do with it, because only there
    is that output otherwise unreachable -- see ``_elevation_transcript()``.

    Three different mechanisms because the platforms genuinely differ, not
    because three felt thorough: macOS has one system dialog for exactly
    this and ``maybe_auto_enable_macos()`` already uses it, Windows has UAC
    and nothing else, and Linux has whatever the desktop installed -- which
    is usually polkit and is sometimes nothing at all. The None case is that
    last one, and it is the reason this returns rather than guessing: a
    ``sudo`` with no askpass in a systemd-started process hangs on a
    password prompt nobody can see.

    The three argv builders (``_macos_admin_argv``/``_windows_runas_argv``/
    ``_pkexec_argv``) are shared with ``service_control.py``'s ``daemon
    start/stop/restart`` (the local-mode-fixes plan's Phase 2) -- this function is only what
    composes the ``enable --for-user`` command line each of them runs.
    """
    platform = current_platform()
    if platform == "darwin":
        # Deliberately not ``--auto``, which ``maybe_auto_enable_macos()``
        # does pass: that mode exits 0 on every failure path, which is right
        # for a prompt nobody asked for and wrong here, where a failure has
        # to reach the caller rather than be reported to the human as a
        # completed step they should now log out for.
        command = f"{shlex.quote(str(script))} enable --for-user {shlex.quote(user)}"
        return _macos_admin_argv(command)
    if platform == "win32":
        # -Verb RunAs is UAC: it re-launches elevated, which is why this
        # cannot simply be the inner argv. -Wait -PassThru so the return code
        # below is the script's own rather than the launcher's, and
        # ``transcript`` so a failure says why. All three live in
        # _windows_runas_argv(), along with the quoting neither caller may
        # get wrong on its own.
        return _windows_runas_argv(
            script, f"enable -ForUser {_powershell_quoted(user)}", transcript
        )
    return _pkexec_argv(script, "enable", "--for-user", user)


def complete_per_user_separation(user: str | None = None) -> bool:
    """Run this platform's ``enable --for-user`` elevated, for ``user``
    (default: whoever is running this process), and report whether it took.

    Every failure is logged rather than raised, and every one of them names
    the command that would have done it by hand: this runs from the
    companion's own start, where there is nothing useful to propagate an
    exception to and where the worst outcome is a person who never finds out
    their install is a group membership short of working.
    """
    state = separation()
    if state is None:
        return False
    account = user or current_user_name()
    if not account:  # pragma: no cover -- current_user_name() falls back to the uid
        return False
    script = installer_script_path()
    if script is None:
        logger.warning(
            "%s is not in the %s group and this install has no provisioning script to run "
            "-- see docs/platform-support.md.", account, state.service_group,
        )
        return False
    problem = _elevation_script_problem(script)
    if problem is not None:
        logger.warning(
            "not completing privilege separation for %s: %s. Run it by hand instead: %s",
            account, problem, per_user_command_text(script, account),
        )
        return False
    with contextlib.ExitStack() as stack:
        transcript = stack.enter_context(_elevation_transcript())
        argv = _per_user_argv(script, account, transcript)
        if argv is None:
            logger.warning(
                "%s is not in the %s group yet and there is no way to ask for a password from "
                "here (no pkexec). Run: %s",
                account, state.service_group, per_user_command_text(script, account),
            )
            return False
        try:
            result = subprocess.run(  # nosec B603  # fixed argv built above, no shell, quoted per layer
                argv, capture_output=True, text=True, timeout=300, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("could not complete privilege separation for %s", account, exc_info=True)
            return False
        # Inside the ExitStack: the transcript is removed on the way out.
        detail = _elevation_detail(result, transcript)
    if result.returncode != 0:
        # A declined password dialog lands here and is an expected outcome,
        # not a bug -- the same reading `_run_auto_enable_macos()` takes of
        # osascript's own nonzero exit.
        logger.info(
            "privilege separation was not completed for %s: %s. Run it by hand: %s",
            account, detail, per_user_command_text(script, account),
        )
        return False
    reset_cache()
    # The same re-check `_run_full_auto_enable_non_macos()` makes, for the
    # same reason (privacyfence/privacyfence#599) and with the same
    # belt-and-braces relationship to the exit code above: returning True
    # here makes the companion tell somebody to log out and back in, and a
    # log-out that fixes nothing is worse advice than none. Only a group
    # that could not be read at all is taken on trust -- the fallback
    # `owner_membership_pending()` already takes, and for the same reason:
    # guessing on a platform just failed to interrogate would have the
    # companion re-prompt at every start.
    members = service_group_members(state.service_group)
    if members is not None and not any(accounts_equal(member, account) for member in members):
        logger.error(
            "privilege separation for %s exited 0 but %s is still not in the %s group. "
            "What the elevated run said: %s. Run it by hand: %s",
            account, account, state.service_group, detail,
            per_user_command_text(script, account),
        )
        return False
    return True
