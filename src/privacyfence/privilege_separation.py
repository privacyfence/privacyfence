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
agent runs as the logged-in user, the daemon runs as ``_privacyfence``, and
the human-authority files are ``0700`` under the latter.

**macOS only, for now, and opt-in.** ``SUPPORTED_PLATFORMS`` is the single
gate: on Linux and Windows every function here reports "not separated" and
every path in ``paths.py`` resolves exactly as it did before this module
existed, byte for byte. #428 P4's Linux (B5b) and Windows (B5c) phases add
their platform to that tuple along with the installer that can actually
provision it -- Windows in particular needs net-new NTFS ACL work that has
no equivalent here, which is why macOS goes first: real POSIX permissions
already work, so this platform validates the shape at the lowest cost.

## The layout

``scripts/macos_privilege_separation.sh enable`` is what provisions this; it
is the only supported way to turn it on, and it writes the marker file this
module reads. Afterwards::

    /Library/Application Support/PrivacyFence      _privacyfence:_privacyfence  0711
    ├── privilege-separation.json                  _privacyfence:_privacyfence  0644
    ├── authority/                                 _privacyfence:_privacyfence  0700
    │   ├── config/settings.yaml                     <- policy the agent may not edit
    │   ├── webauthn_credentials.json                <- #426's store, now unforgeable
    │   └── logs/audit/                              <- and its HMAC key
    ├── credentials/, logs/, ...                   _privacyfence:_privacyfence  0700
    └── handoff/                                   _privacyfence:_privacyfence  2770
        ├── mcp_token, mcp_url                       <- the agent's own credential
        ├── web_base_url, *_url                      <- discovery files a human reads
        ├── control.sock                             <- daemon listens, companion connects
        └── companion.sock                           <- companion listens, daemon connects

The root is ``0711``: traversable by anyone, listable by no one, so a process
running as the logged-in user can reach ``handoff/`` without being able to
enumerate anything else. ``handoff/`` is group-owned by ``_privacyfence``
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
LaunchDaemon; issue #428's "Honest limits" says so, and so does
``docs/security-and-compliance.md``. The guarantee is against an agent
running with the user's *normal* privileges, which is the ordinary case, and
it makes escalation require an authentication prompt a human sees.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from . import secure_files

logger = logging.getLogger(__name__)

# The dedicated account the daemon runs as. Underscore-prefixed per Apple's
# own convention for hidden system accounts (``_www``, ``_spotlight``, ...),
# which is also what keeps it out of the login window and Users & Groups.
SERVICE_ACCOUNT_NAME = "_privacyfence"
SERVICE_GROUP_NAME = "_privacyfence"

# Where a separated install keeps everything ``paths.data_dir()`` used to put
# under ``~/.privacyfence``. A service account cannot sensibly own something
# inside a human's home directory -- the same reasoning #428 gives for
# Windows having to move out of ``%LOCALAPPDATA%`` into ``%ProgramData%``.
MACOS_SYSTEM_ROOT = Path("/Library/Application Support/PrivacyFence")

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

# #428 P4 ships per platform (B5a/B5b/B5c) rather than as one "x3 platforms"
# phase, so macOS and Linux can land and soak even if Windows' net-new ACL
# work runs long. Adding a platform here without its installer would make
# every process on it look for a marker nothing can write.
SUPPORTED_PLATFORMS = ("darwin",)


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


def platform_supported() -> bool:
    return current_platform() in SUPPORTED_PLATFORMS


def system_root() -> Path | None:
    """Where a separated install's root *would* be on this platform, whether
    or not one has been provisioned -- None on a platform #428 P4 hasn't
    shipped for yet, which is what makes every other function here a cheap
    no-op there."""
    override = os.environ.get(SYSTEM_ROOT_ENV_VAR)
    if override:
        # An override that isn't absolute would resolve differently per
        # process depending on each one's cwd -- the daemon's is set by its
        # LaunchDaemon, the companion's by whatever launched it. Rejecting
        # it outright beats half the install silently using a different root.
        candidate = Path(override)
        if not candidate.is_absolute():
            logger.warning(
                "%s=%r is not an absolute path -- ignoring it and using the default layout.",
                SYSTEM_ROOT_ENV_VAR, override,
            )
        else:
            return candidate
    if not platform_supported():
        return None
    return MACOS_SYSTEM_ROOT


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
    silent on failure, like every other permission fix-up here."""
    try:
        if path.stat().st_mode & 0o7777 != handoff_file_mode():
            path.chmod(handoff_file_mode())
    except OSError as exc:  # pragma: no cover -- best effort, same posture as secure_mkdir
        logger.warning("Could not set permissions on %s: %s", path, exc)


def current_user_name() -> str:
    """This process's account name, or its numeric uid as a string where
    ``pwd`` can't answer (a uid with no passwd entry -- possible inside a
    container, and Windows has no ``pwd`` module at all)."""
    if os.name == "nt":  # pragma: no cover -- Windows has no pwd module
        return os.environ.get("USERNAME", "")
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:  # pragma: no cover -- a uid with no passwd entry
        return str(os.geteuid())


def running_as_service_account() -> bool:
    state = separation()
    return state is not None and current_user_name() == state.service_account


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
    state = separation()
    if state is None:
        path = marker_path()
        if path is not None and path.exists():
            raise PrivilegeSeparationError(
                f"{path} exists but could not be interpreted by this version of PrivacyFence "
                "(see the errors logged above). Refusing to start: continuing would resolve the "
                "un-separated data directory and silently ignore the policy, passkeys and audit "
                "log stored under the separated one. Re-run "
                "'sudo scripts/macos_privilege_separation.sh status' to inspect the install."
            )
        return
    actual = current_user_name()
    if actual != state.service_account:
        raise PrivilegeSeparationError(
            f"This install runs the daemon under the dedicated {state.service_account!r} account "
            f"(#428 Phase 4), but this process is running as {actual!r}. Refusing to start: the "
            f"human-authority files under {state.authority_dir} are not readable as {actual!r}, so "
            "starting would seed a fresh default policy and ignore the real one. Start the daemon "
            "via its LaunchDaemon ('sudo launchctl kickstart -k system/com.privacyfence.daemon') "
            "rather than directly."
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


def _authority_owner_problem(state: Separation) -> str | None:
    """The check that actually matters: ``authority/`` being ``0700`` buys
    nothing if it is ``0700`` *under the logged-in user's own uid*, which is
    precisely the pre-#428 state this phase exists to leave behind."""
    if os.name == "nt":  # pragma: no cover -- B5c's problem, and not a POSIX one
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
