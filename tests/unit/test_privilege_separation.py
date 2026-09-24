"""#428 Phase 4 (B5a/B5b/B5c): all three privilege-separation layouts.

The thing under test is a *contract between four artifacts*: the Python
module that resolves paths from a marker file, the shell script that writes
that marker and provisions the layout it describes, the service templates
that start the two processes it splits apart, and the MCPB shim that has to
find the daemon afterwards. Nothing in this repo's CI can run the real thing
-- provisioning it needs root on a macOS or Linux host, and the security
property it buys only exists once two real OS accounts are involved -- so
these tests cover the two halves CI *can* prove: that every path and mode
decision follows from the marker exactly as intended, and that the scripts,
the templates and the shim still agree with the module about what that
marker means.

Everything that isn't a name is shared between the platforms, which is why
almost every test here runs against all of them: what B5b added to B5a is
three strings (a root, an account, a group) and a second installer, and a
test that only ever exercised one platform's strings would not have noticed
the other's going wrong.

B5c is the exception that proves how far that goes. Windows shares the
marker, the directory layout, the migration list and every path decision --
so it joins ``PLATFORMS`` and runs all of that unchanged -- but it expresses
the *permissions* as NTFS ACLs rather than as mode bits, and it provisions
them from PowerShell rather than from bash. So the installer contract and
the layout audit split in two: ``POSIX_PLATFORMS`` keeps the shell-script
and mode-bit assertions, and ``TestWindows*`` below covers the half that has
no POSIX counterpart at all -- the ACL audit, the ``.ps1``, the companion
Scheduled Task, and the service. The one check every platform needs turns
out not to be Windows-only after all: B1 found that a packaged macOS
install's own image can be just as writable by the account it's separated
from as an unelevated Windows one, so ``TestPosixImageAudit`` below covers
the ``stat``-walk counterpart to ``TestWindowsLayoutAudit``'s ACL read.

``current_platform`` is monkeypatched rather than ``sys.platform`` itself,
and ``PRIVACYFENCE_SYSTEM_ROOT`` relocates the whole layout under
``tmp_path`` -- see those two names' own docstrings for why each exists.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from privacyfence import (
    daemon_main,
    paths,
    privilege_separation,
    secure_files,
    windows_acl,
    windows_service,
)
from privacyfence.web import control_channel, mcp_auth

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every platform #428 P4 has shipped for, and the installer that provisions
# it. Kept as its own table rather than derived from PLATFORM_LAYOUTS so a
# platform added to the module without an installer fails here loudly --
# which is exactly the mistake PLATFORM_LAYOUTS' own comment warns about.
PLATFORMS = ("darwin", "linux", "win32")
# The two whose layout is POSIX permission bits, provisioned by a shell
# script. Windows' installer is PowerShell and its layout is ACLs, so every
# assertion that reads a mode or a `NAME="value"` line belongs here rather
# than in PLATFORMS -- see this module's own docstring.
POSIX_PLATFORMS = ("darwin", "linux")
INSTALLERS = {
    "darwin": REPO_ROOT / "scripts" / "macos_privilege_separation.sh",
    "linux": REPO_ROOT / "scripts" / "linux_privilege_separation.sh",
    "win32": REPO_ROOT / "scripts" / "windows_privilege_separation.ps1",
}
MACOS_TEMPLATE_DIR = REPO_ROOT / "installer" / "macos"
LINUX_TEMPLATE_DIR = REPO_ROOT / "installer" / "linux"
WINDOWS_TEMPLATE_DIR = REPO_ROOT / "installer" / "windows"
WINDOWS_INNO_SETUP = REPO_ROOT / "installer" / "privacyfence.iss"
SHIM_PROTOCOL = REPO_ROOT / "mcpb" / "shim" / "src" / "protocol.ts"

# Applied per class, not to the whole module: the marker parsing, the path
# resolution that follows from it and the installer/template contract are all
# pure logic worth running on every platform. What can't run *on* Windows is
# anything that reads a POSIX mode or a file owner back off disk: Windows has
# neither (chmod there is the documented no-op secure_files.py's own docstring
# describes), the same known, accepted gap test_secure_files.py already skips
# for. What that platform has instead is NTFS ACLs, covered by TestWindowsAcl*
# below and -- against a real filesystem -- by
# tests/platform/test_windows_acls.py.
posix_permissions_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="reads POSIX ownership/permission bits back off disk -- Windows has none, and its own layout is asserted through windows_acl instead",
)


def this_account() -> str:
    """This process's own account name. The ``pwd`` module is imported here
    rather than at module scope because it does not exist on Windows, where
    this file is still collected -- every caller below sits behind
    ``posix_permissions_only``."""
    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name


@pytest.fixture(params=PLATFORMS)
def platform_name(request, monkeypatch) -> str:
    """Pretend to be each shipped platform in turn. Requested on its own by
    tests that need to know which one they are on, and pulled in implicitly
    by ``separated`` below -- so a test taking that fixture runs twice, once
    per platform, without saying so."""
    monkeypatch.setattr(privilege_separation, "current_platform", lambda: request.param)
    privilege_separation.reset_cache()
    return request.param


def _marker_payload(platform: str, **overrides) -> dict:
    layout = privilege_separation.PLATFORM_LAYOUTS[platform]
    payload = {
        "version": privilege_separation.MARKER_VERSION,
        "platform": platform,
        "service_account": layout.service_account,
        "service_group": layout.service_group,
        "owner_user": "alice",
        "enabled_at": "2026-09-16T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def _write_marker(root: Path, platform: str = "darwin", **overrides) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / privilege_separation.MARKER_FILE_NAME).write_text(
        json.dumps(_marker_payload(platform, **overrides)), encoding="utf-8"
    )
    privilege_separation.reset_cache()


@pytest.fixture
def separated(platform_name, monkeypatch, tmp_path):
    """A provisioned separated install under ``tmp_path``, as that platform's
    installer would leave it: the marker, the three directories, and the
    modes. Yields the system root."""
    root = tmp_path / "PrivacyFence"
    monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
    root.mkdir(parents=True)
    (root / "authority").mkdir()
    (root / privilege_separation.HANDOFF_DIR_NAME).mkdir()
    _write_marker(root, platform_name)
    root.chmod(privilege_separation.SYSTEM_ROOT_MODE)
    (root / "authority").chmod(privilege_separation.AUTHORITY_DIR_MODE)
    (root / privilege_separation.HANDOFF_DIR_NAME).chmod(privilege_separation.HANDOFF_DIR_MODE)
    privilege_separation.reset_cache()
    yield root
    privilege_separation.reset_cache()


def service_account_of(platform: str) -> str:
    return privilege_separation.PLATFORM_LAYOUTS[platform].service_account


class TestNotSeparated:
    """The default, and the one that matters most: an install nobody has
    opted in has to behave byte-identically to how it did before this module
    existed."""

    def test_disabled_with_no_marker(self, platform_name, monkeypatch, tmp_path):
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing-here"))
        privilege_separation.reset_cache()

        assert privilege_separation.is_enabled() is False
        assert privilege_separation.data_dir_override() is None

    def test_disabled_on_an_unsupported_platform(self, monkeypatch):
        # All three desktop platforms have an installer as of B5c, so this is
        # now about the ones that never will: nothing could have written a
        # marker on FreeBSD, and looking for one would mean reading a path
        # this module invented.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "freebsd")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()

        assert privilege_separation.platform_layout() is None
        assert privilege_separation.system_root() is None
        assert privilege_separation.marker_path() is None
        assert privilege_separation.is_enabled() is False

    def test_handoff_dir_is_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        privilege_separation.reset_cache()

        assert paths.handoff_dir() == tmp_path

    def test_control_socket_stays_under_authority(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "authority_dir", lambda: tmp_path / "authority")
        privilege_separation.reset_cache()

        assert paths.control_socket_dir() == tmp_path / "authority"

    def test_modes_are_the_ordinary_ones(self):
        assert privilege_separation.socket_mode() == 0o600
        assert privilege_separation.handoff_dir_mode() == secure_files.DEFAULT_DIR_MODE
        assert privilege_separation.handoff_file_mode() == secure_files.DEFAULT_FILE_MODE

    def test_audit_layout_has_nothing_to_say(self):
        assert privilege_separation.audit_layout() == []


class TestMarkerParsing:
    """Every rejection here ends in the same place -- the un-separated layout
    -- which is precisely why check_runtime_identity() refuses to start on a
    marker that exists but didn't parse. Silently resolving the wrong
    directory is the failure mode all of this exists to avoid."""

    @pytest.fixture(autouse=True)
    def _under_tmp(self, platform_name, monkeypatch, tmp_path):
        self.platform = platform_name
        self.root = tmp_path / "PrivacyFence"
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(self.root))
        privilege_separation.reset_cache()
        yield
        privilege_separation.reset_cache()

    def test_accepts_a_well_formed_marker(self):
        _write_marker(self.root, self.platform)

        state = privilege_separation.separation()

        assert state is not None
        assert state.service_account == service_account_of(self.platform)
        assert state.owner_user == "alice"
        # Derived from where the marker was found, never stored in it, so the
        # two can't disagree.
        assert state.data_dir == self.root
        assert state.authority_dir == self.root / "authority"
        assert state.handoff_dir == self.root / "handoff"

    def test_rejects_a_future_version(self):
        _write_marker(self.root, self.platform, version=privilege_separation.MARKER_VERSION + 1)

        assert privilege_separation.separation() is None

    def test_rejects_another_platforms_marker(self):
        # Including the *other shipped* platform's, not just a fictional one:
        # a /var/lib directory restored onto a Mac, or a home directory synced
        # between the two, carries account names that mean nothing here.
        other = next(p for p in PLATFORMS if p != self.platform)
        _write_marker(self.root, other)

        assert privilege_separation.separation() is None

    def test_rejects_a_marker_missing_a_field(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / privilege_separation.MARKER_FILE_NAME).write_text(
            json.dumps({"version": 1, "platform": self.platform}), encoding="utf-8"
        )
        privilege_separation.reset_cache()

        assert privilege_separation.separation() is None

    def test_rejects_malformed_json(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / privilege_separation.MARKER_FILE_NAME).write_text("{not json", encoding="utf-8")
        privilege_separation.reset_cache()

        assert privilege_separation.separation() is None

    def test_rejects_a_json_scalar(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / privilege_separation.MARKER_FILE_NAME).write_text('"nope"', encoding="utf-8")
        privilege_separation.reset_cache()

        assert privilege_separation.separation() is None

    def test_answer_is_cached_within_a_process(self):
        _write_marker(self.root, self.platform)
        assert privilege_separation.is_enabled() is True

        (self.root / privilege_separation.MARKER_FILE_NAME).unlink()

        # Not a quirk being pinned down for its own sake: paths.data_dir()
        # calls this on every credential read, and the answer genuinely
        # cannot change under a running daemon -- enabling or disabling
        # restarts it.
        assert privilege_separation.is_enabled() is True
        privilege_separation.reset_cache()
        assert privilege_separation.is_enabled() is False


class TestSystemRootOverride:
    def test_ignores_a_relative_override(self, platform_name, monkeypatch):
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, "relative/path")
        privilege_separation.reset_cache()

        # A relative root would resolve differently per process depending on
        # each one's cwd -- the daemon's comes from its LaunchDaemon/systemd
        # unit, the companion's from whatever launched it -- so half an
        # install would silently use a different directory.
        assert privilege_separation.system_root() == (
            privilege_separation.PLATFORM_LAYOUTS[platform_name].system_root
        )

    @pytest.mark.parametrize(
        "platform,expected",
        [
            ("darwin", "/Library/Application Support/PrivacyFence"),
            # FHS 3.0 §5.8. Not ~/.privacyfence anywhere on the system and not
            # /opt: this is variable state the daemon rewrites, while /opt
            # holds the (read-only, dpkg-owned) application bundle itself.
            ("linux", "/var/lib/privacyfence"),
            # Per-machine application state, outside every user profile --
            # which is exactly what %LOCALAPPDATA% is not, and the whole
            # reason the Windows data directory has to move at all.
            ("win32", "C:/ProgramData/PrivacyFence"),
        ],
    )
    def test_default_root_per_platform(self, platform, expected, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: platform)
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        monkeypatch.delenv(privilege_separation.WINDOWS_PROGRAM_DATA_ENV_VAR, raising=False)
        privilege_separation.reset_cache()

        assert privilege_separation.system_root() == Path(expected)

    def test_windows_prefers_the_real_program_data(self, monkeypatch):
        # %ProgramData% can be redirected to another volume, and the
        # installer's icacls runs against wherever it really is -- resolving
        # the hardcoded C: there would leave every process looking somewhere
        # nothing was provisioned. The shim makes the same choice
        # (protocol.ts's defaultSystemRoot).
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        monkeypatch.setenv(privilege_separation.WINDOWS_PROGRAM_DATA_ENV_VAR, "D:/ProgramData")
        privilege_separation.reset_cache()

        assert privilege_separation.system_root() == Path("D:/ProgramData/PrivacyFence")

    def test_only_windows_consults_program_data(self, monkeypatch):
        # The variable exists on a Windows machine only, but nothing stops a
        # POSIX process from having it set -- and a macOS install reading it
        # would relocate its whole layout on the strength of a stray
        # environment variable.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        monkeypatch.setenv(privilege_separation.WINDOWS_PROGRAM_DATA_ENV_VAR, "D:/ProgramData")
        privilege_separation.reset_cache()

        assert privilege_separation.system_root() == privilege_separation.MACOS_SYSTEM_ROOT

    def test_refuses_the_override_when_the_real_root_has_a_marker(self, platform_name, monkeypatch, tmp_path):
        # B11: on a separated install, the companion and the MCPB shim honour
        # this var too, and their environment comes from the user's login
        # session -- exactly the boundary privilege separation exists to
        # hold. Once a real install is provisioned at the platform's actual
        # root, a user-session process redirecting itself elsewhere is the
        # attack this guards against, not the test hatch the variable is for.
        real_root = tmp_path / "real"
        _write_marker(real_root, platform_name)
        monkeypatch.setattr(privilege_separation, "_default_system_root", lambda: real_root)
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "attacker-controlled"))
        privilege_separation.reset_cache()

        assert privilege_separation.system_root() == real_root

    def test_still_honours_the_override_when_the_real_root_has_no_marker(self, platform_name, monkeypatch, tmp_path):
        # The common case, and the one the escape hatch is actually for: a
        # dev/CI machine has never had a real install provisioned at its
        # platform's literal system root (that needs root to create), so the
        # override still works exactly as before.
        monkeypatch.setattr(privilege_separation, "_default_system_root", lambda: tmp_path / "never-provisioned")
        test_root = tmp_path / "test"
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(test_root))
        privilege_separation.reset_cache()

        assert privilege_separation.system_root() == test_root

    def test_refusal_falls_back_to_the_real_root_not_none(self, platform_name, monkeypatch, tmp_path):
        # Refusing the override must not also refuse separation itself --
        # the process should behave as if the variable were never set, i.e.
        # use the real, already-provisioned root, not fail closed to None.
        real_root = tmp_path / "real"
        _write_marker(real_root, platform_name)
        monkeypatch.setattr(privilege_separation, "_default_system_root", lambda: real_root)
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "attacker-controlled"))
        privilege_separation.reset_cache()

        assert privilege_separation.is_enabled() is True
        assert privilege_separation.separation().data_dir == real_root

    @pytest.mark.parametrize(
        "platform,expected",
        [
            # Apple's hidden-system-account convention (_www, _spotlight, ...).
            ("darwin", "_privacyfence"),
            # And emphatically not that on Linux, where the underscore is not
            # a convention and would read as a typo -- `useradd --system`'s
            # sub-UID_MIN id is what hides the account there.
            ("linux", "privacyfence"),
        ],
    )
    def test_service_account_name_per_platform(self, platform, expected):
        layout = privilege_separation.PLATFORM_LAYOUTS[platform]

        assert layout.service_account == expected
        assert layout.service_group == expected

    def test_windows_account_and_group_are_two_different_principals(self):
        # The one place the three layouts genuinely differ in shape rather
        # than in spelling. On POSIX the daemon is a member of its own group,
        # so one name does both jobs. A Windows virtual service account has
        # no group memberships at all, so the group is a separate local group
        # the human is added to -- and every ACL and pipe DACL has to name
        # both principals rather than relying on membership to cover one.
        layout = privilege_separation.PLATFORM_LAYOUTS["win32"]

        assert layout.service_account == "NT SERVICE\\PrivacyFence"
        assert layout.service_group == "PrivacyFenceUsers"
        assert layout.service_account != layout.service_group

    def test_the_windows_account_name_follows_the_service_name(self):
        # Not a naming choice: Windows derives a virtual account's name from
        # its service's, so `sc create PrivacyFence obj= NT SERVICE\Something
        # Else` simply is not a virtual account. These two constants are one
        # fact written twice.
        assert privilege_separation.WINDOWS_SERVICE_ACCOUNT_NAME == (
            f"NT SERVICE\\{privilege_separation.WINDOWS_SERVICE_NAME}"
        )


class TestSeparatedPathResolution:
    pytestmark = posix_permissions_only

    def test_data_dir_is_the_system_root(self, separated):
        assert paths.data_dir() == separated

    def test_handoff_dir_is_a_subdirectory_of_it(self, separated):
        assert paths.handoff_dir() == separated / "handoff"

    def test_authority_dir_moves_with_it(self, separated):
        # The whole point: settings.yaml, webauthn_credentials.json and the
        # audit log's HMAC key now live under a root the agent's account does
        # not own.
        assert paths.authority_dir() == separated / "authority"

    def test_control_socket_moves_out_of_authority(self, separated):
        # authority/ is 0700 under the service account, so a companion
        # running as the human could not connect to a socket inside it.
        assert paths.control_socket_dir() == separated / "handoff"
        # Compared against socket_path_under()'s own answer for that directory
        # rather than a literal ``.../handoff/control.sock``: that function
        # falls back to a short name in the system temp directory when the
        # preferred path would overflow AF_UNIX's sun_path, which a macOS
        # runner's own tmp_path reliably does (/private/var/folders/...) and a
        # Linux one reliably doesn't. What Phase 4 changes is *which directory*
        # is consulted, and that holds in either regime.
        assert control_channel.posix_socket_path() == control_channel.socket_path_under(separated / "handoff")
        assert control_channel.posix_socket_path() != control_channel.socket_path_under(separated / "authority")

    def test_companion_socket_is_in_the_handoff_dir(self, separated):
        assert control_channel.companion_socket_path() == control_channel.companion_socket_path_under(
            separated / "handoff"
        )
        # Pre-Phase-4 this was rooted at data_dir() itself. It had to move: the
        # companion runs as the human, who cannot create anything under the
        # service-account-owned root.
        assert control_channel.companion_socket_path() != control_channel.companion_socket_path_under(separated)

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_a_real_installs_sockets_fit_in_sun_path(self, platform):
        # The two comparisons above are deliberately fallback-agnostic, which
        # means they would also pass if the shipped layout were too long for
        # AF_UNIX and every socket silently landed in /tmp instead. Neither
        # shipped root is, and that is a property of the directory names these
        # phases chose rather than an accident -- so assert it against both.
        handoff = (
            privilege_separation.PLATFORM_LAYOUTS[platform].system_root
            / privilege_separation.HANDOFF_DIR_NAME
        )

        assert control_channel.socket_path_under(handoff) == handoff / "control.sock"
        assert control_channel.companion_socket_path_under(handoff) == handoff / "companion.sock"

    def test_mcp_token_moved_out_of_the_shared_handoff_dir(self, separated):
        # ADR 0008 retires #428's "mcp_token stays reachable by the agent"
        # invariant on purpose: a token any service-group member could read
        # off disk is exactly the shared-identity leak Phase 3 closes. It
        # now lives under authority_dir() -- service-account-owned, 0700,
        # reachable only by minting it fresh over the control channel
        # (MINT MCP) -- not under the shared, agent-readable handoff dir.
        token = mcp_auth.load_or_create_mcp_token()

        path = separated / "authority" / "mcp_token"
        assert path.read_text(encoding="utf-8") == token
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not (separated / "handoff" / "mcp_token").exists()

    def test_resolving_paths_does_not_retighten_the_handoff_dir(self, separated):
        # secure_mkdir re-asserts its mode on an existing directory, which is
        # right everywhere except here: 0700 would lock out the very accounts
        # the installer just let in, one discovery-file write at a time.
        paths.handoff_dir()
        mcp_auth.load_or_create_mcp_token()
        paths.handoff_dir()

        mode = stat.S_IMODE((separated / "handoff").stat().st_mode)
        assert mode == privilege_separation.HANDOFF_DIR_MODE

    def test_data_dir_resolution_keeps_the_traversable_root_mode(self, separated):
        paths.data_dir()

        assert stat.S_IMODE(separated.stat().st_mode) == privilege_separation.SYSTEM_ROOT_MODE

    def test_socket_mode_opens_to_the_shared_group(self, separated):
        # connect(2) on a unix socket needs write permission on the node, so
        # the group bits have to be rw.
        assert privilege_separation.socket_mode() == 0o660


class TestRuntimeIdentity:
    def test_unseparated_install_is_always_fine(self, platform_name, monkeypatch, tmp_path):
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "absent"))
        privilege_separation.reset_cache()

        privilege_separation.check_runtime_identity()

    def test_accepts_the_service_account(self, separated, platform_name, monkeypatch):
        monkeypatch.setattr(
            privilege_separation, "current_user_name", lambda: service_account_of(platform_name)
        )

        privilege_separation.check_runtime_identity()

    def test_refuses_the_logged_in_user(self, separated, platform_name, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "alice")

        with pytest.raises(privilege_separation.PrivilegeSeparationError) as exc:
            privilege_separation.check_runtime_identity()
        message = str(exc.value)
        assert service_account_of(platform_name) in message
        # And says what to do instead, in this platform's own words -- a
        # `launchctl kickstart` hint on a Linux box is worse than none.
        assert privilege_separation.PLATFORM_LAYOUTS[platform_name].start_command in message

    def test_refuses_a_marker_it_could_not_parse(self, platform_name, monkeypatch, tmp_path):
        # The dangerous case: paths.py would resolve the *un*separated layout
        # while a separated one sits on disk, and load_config()'s own first-run
        # behavior would seed a fresh default policy over the real one.
        root = tmp_path / "PrivacyFence"
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
        _write_marker(root, platform_name, version=99)

        with pytest.raises(privilege_separation.PrivilegeSeparationError) as exc:
            privilege_separation.check_runtime_identity()
        # Names the command that can inspect it, which differs per platform.
        assert privilege_separation.PLATFORM_LAYOUTS[platform_name].status_command in str(exc.value)


class TestStopCommand:
    """#428 B4: the control channel's ``QUIT`` names this instead of acting,
    once separated -- one per platform, same as ``start_command``."""

    def test_every_platform_has_one(self, platform_name):
        layout = privilege_separation.PLATFORM_LAYOUTS[platform_name]
        assert layout.stop_command
        assert layout.stop_command != layout.start_command

    def test_names_this_platforms_own_service_manager(self, platform_name):
        expected_tool = {"darwin": "launchctl", "linux": "systemctl", "win32": "sc.exe"}
        layout = privilege_separation.PLATFORM_LAYOUTS[platform_name]
        assert expected_tool[platform_name] in layout.stop_command


class TestAuditLayout:
    pytestmark = posix_permissions_only

    @pytest.fixture(autouse=True)
    def _modes_are_the_primitive_here(self, platform_name):
        # audit_layout() reads POSIX modes on macOS/Linux and NTFS ACLs on
        # Windows -- two different primitives, not one with a branch -- so
        # the mode assertions below have nothing to say about the win32 leg
        # of ``platform_name``. Its equivalents are TestWindowsLayoutAudit.
        if platform_name == "win32":
            pytest.skip("Windows' layout audit is ACLs -- see TestWindowsLayoutAudit")

    def test_clean_layout_reports_nothing(self, separated, monkeypatch):
        monkeypatch.setattr(privilege_separation, "_authority_owner_problem", lambda _state: None)

        assert privilege_separation.audit_layout() == []

    def test_reports_a_loosened_authority_dir(self, separated, monkeypatch):
        monkeypatch.setattr(privilege_separation, "_authority_owner_problem", lambda _state: None)
        (separated / "authority").chmod(0o755)

        problems = privilege_separation.audit_layout()

        assert len(problems) == 1
        assert "authority" in problems[0]

    def test_reports_an_authority_dir_the_human_still_owns(self, separated):
        # 0700 under the logged-in user's own uid is exactly the pre-#428
        # state this phase exists to leave behind, and is invisible to a
        # mode-only check.
        problems = privilege_separation.audit_layout()

        assert any("not actually in effect" in problem for problem in problems)


class TestProcessIdentityHelpers:
    pytestmark = posix_permissions_only

    def test_current_user_name_answers_this_process(self):
        # Thin, but it is what check_runtime_identity()'s whole decision rests
        # on, and the import of ``pwd`` inside it is the part that would break
        # silently on a build where it isn't available.
        assert privilege_separation.current_user_name() == this_account()

    def test_running_as_service_account_is_false_when_unseparated(self):
        privilege_separation.reset_cache()

        assert privilege_separation.running_as_service_account() is False

    def test_running_as_service_account_follows_the_marker(self, separated, platform_name, monkeypatch):
        monkeypatch.setattr(
            privilege_separation, "current_user_name", lambda: service_account_of(platform_name)
        )
        assert privilege_separation.running_as_service_account() is True

        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "alice")
        assert privilege_separation.running_as_service_account() is False


class TestServiceAccountUid:
    """#428 B10: ``web/control_channel.py``'s companion channel checks a
    connecting peer's real uid against this."""

    # Two of the tests below call the real `pwd.getpwnam` (POSIX-only, like
    # TestProcessIdentityHelpers above) -- applied to the whole class rather
    # than just those two so a `platform_name`-parametrized method skips the
    # same way on a real Windows runner regardless of which platform it's
    # simulating, matching that class's own convention.
    pytestmark = posix_permissions_only

    def test_none_when_unseparated(self):
        privilege_separation.reset_cache()

        assert privilege_separation.service_account_uid() is None

    def test_none_on_windows_even_when_separated(self, separated):
        # win32 is one of the three platforms `separated` parametrizes over
        # (see platform_name); a uid has no meaning there at all -- the
        # boundary is an ACL, checked a different way entirely.
        if privilege_separation.current_platform() != "win32":
            pytest.skip("only the win32 parametrization of `separated` exercises this")

        assert privilege_separation.service_account_uid() is None

    def test_resolves_a_real_account_on_posix(self, separated, platform_name):
        if platform_name == "win32":
            pytest.skip("POSIX only -- see test_none_on_windows_even_when_separated")
        import pwd

        # Overwrite the marker `separated` already wrote so it names this
        # test process's own (real, existing) account instead of the
        # platform's ordinary service-account name.
        _write_marker(separated, platform_name, service_account=this_account())

        assert privilege_separation.service_account_uid() == pwd.getpwnam(this_account()).pw_uid

    def test_none_for_an_account_that_does_not_exist(self, separated, platform_name):
        if platform_name == "win32":
            pytest.skip("POSIX only -- see test_none_on_windows_even_when_separated")

        _write_marker(separated, platform_name, service_account="privacyfence-b10-test-no-such-account")

        assert privilege_separation.service_account_uid() is None


class TestAuditLayoutBestEffort:
    """Every probe here is best-effort on purpose: the process running the
    audit may legitimately not be able to see a path (that is half the point
    of the layout), and a permission error there must not be reported as a
    layout defect or crash a startup check."""

    pytestmark = posix_permissions_only

    @pytest.fixture(autouse=True)
    def _modes_are_the_primitive_here(self, platform_name):
        if platform_name == "win32":
            pytest.skip("Windows' layout audit is ACLs -- see TestWindowsLayoutAudit")

    def test_a_missing_directory_is_skipped_rather_than_reported(self, separated, monkeypatch):
        monkeypatch.setattr(privilege_separation, "_authority_owner_problem", lambda _state: None)
        (separated / "handoff").rmdir()

        assert privilege_separation.audit_layout() == []

    def test_an_unreadable_authority_dir_is_skipped(self, separated, monkeypatch):
        monkeypatch.setattr(privilege_separation, "_describe_mode", lambda _path, _mode: None)
        (separated / "authority").rmdir()

        assert privilege_separation.audit_layout() == []

    def test_an_authority_dir_the_service_account_owns_is_clean(self, separated, platform_name):
        # The passing case for the ownership probe: pretend this process's own
        # account *is* the service account, which is what a real separated
        # install looks like from the daemon's side.
        _write_marker(separated, platform_name, service_account=this_account())

        assert privilege_separation.audit_layout() == []

    def test_an_unreadable_marker_is_reported_but_not_fatal(self, platform_name, monkeypatch, tmp_path):
        # ENOENT is the ordinary "not separated" answer and stays silent; any
        # other OSError means a hand-edited layout and is worth a warning.
        root = tmp_path / "PrivacyFence"
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
        (root / privilege_separation.MARKER_FILE_NAME).mkdir(parents=True)
        privilege_separation.reset_cache()

        assert privilege_separation.separation() is None


class TestPosixImageAudit:
    """B1: nothing previously verified the daemon/companion image was not
    user-writable before privilege separation elevated to it. ADR 0002 §5a
    used to claim ``/Applications`` was root-owned the same way ``/opt`` is
    -- it's actually ``root:admin drwxrwxr-x``, and a drag-installed ``.app``
    is normally owned by the installing user. This is the POSIX counterpart
    of ``TestWindowsLayoutAudit``'s image tests and of
    ``windows_acl.image_problems()`` itself: a ``stat`` walk rather than an
    ACL read, since a mode bit has no inheritance to lean on the way an ACL
    does.
    """

    pytestmark = posix_permissions_only

    @pytest.fixture
    def fake_stats(self, monkeypatch):
        """A controlled ownership/mode table, standing in for the real
        filesystem this test process cannot ``chown`` to root without being
        root. Anything not explicitly set reads back as ``root:root 0755``
        -- an ordinary, trusted directory -- so a test only has to describe
        the one path it cares about, and the ancestor walk the function
        under test does on its own doesn't introduce noise.
        """
        table: dict[Path, os.stat_result] = {}

        def _entry(uid: int, gid: int, mode: int) -> os.stat_result:
            return os.stat_result((stat.S_IFDIR | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))

        def fake_stat(self_path, *args, **kwargs):
            return table.get(self_path, _entry(0, 0, 0o755))

        monkeypatch.setattr(Path, "stat", fake_stat)

        def set_stat(path: Path, *, uid: int = 0, gid: int = 0, mode: int = 0o755) -> None:
            table[path] = _entry(uid, gid, mode)

        return set_stat

    def test_posix_image_paths_to_check_walks_up_to_the_filesystem_root(self):
        image = Path("/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp")

        result = privilege_separation._posix_image_paths_to_check((image,))

        assert image in result
        assert Path("/Applications/PrivacyFenceApp.app") in result
        assert Path("/Applications") in result
        assert Path("/") in result

    def test_posix_image_paths_to_check_dedupes_shared_ancestors(self):
        # Daemon and companion live in the same bundle -- "or any directory
        # on the path to it" should not turn into the same directory
        # reported twice.
        macos_dir = Path("/Applications/PrivacyFenceApp.app/Contents/MacOS")
        daemon = macos_dir / "PrivacyFenceApp"
        companion = macos_dir / "PrivacyFenceCompanion"

        result = privilege_separation._posix_image_paths_to_check((daemon, companion))

        assert result.count(macos_dir) == 1

    def test_a_root_owned_unwritable_image_is_clean(self, fake_stats):
        image = Path("/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp")
        fake_stats(image, uid=0, gid=0, mode=0o755)

        assert privilege_separation._posix_image_problems((image,)) == []

    def test_reports_an_image_not_owned_by_root(self, fake_stats):
        # The exact defect: a drag-installed .app is normally owned by the
        # installing user, the same account the agent runs as.
        image = Path("/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp")
        fake_stats(image, uid=501, gid=20, mode=0o755)

        problems = privilege_separation._posix_image_problems((image,))

        assert len(problems) == 1
        assert str(image) in problems[0]
        assert "not root" in problems[0]

    def test_reports_a_world_writable_image(self, fake_stats):
        image = Path("/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp")
        fake_stats(image, uid=0, gid=0, mode=0o777)

        problems = privilege_separation._posix_image_problems((image,))

        assert len(problems) == 1
        assert "world-writable" in problems[0]

    def test_reports_a_group_writable_image_by_an_untrusted_group(self, fake_stats):
        # /Applications itself, precisely: root:admin drwxrwxr-x.
        applications = Path("/Applications")
        fake_stats(applications, uid=0, gid=80, mode=0o775)

        problems = privilege_separation._posix_image_problems((applications,))

        assert len(problems) == 1
        assert "group-writable" in problems[0]

    def test_a_wheel_group_writable_image_is_trusted(self, monkeypatch, fake_stats):
        import grp

        class _FakeGrpEntry:
            gr_gid = 4242

        monkeypatch.setattr(grp, "getgrnam", lambda name: _FakeGrpEntry())
        image = Path("/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp")
        fake_stats(image, uid=0, gid=4242, mode=0o775)

        assert privilege_separation._posix_image_problems((image,)) == []

    def test_a_missing_path_is_skipped_rather_than_reported(self):
        # Best-effort like every other check in this module: this function
        # never sees a real missing path in the tests above (fake_stats
        # answers for everything), but a real filesystem will have plenty
        # of parents that don't exist as literal directories (e.g. a
        # mount point's own parent).
        image = Path("/this/path/does/not/exist/PrivacyFenceApp")

        assert privilege_separation._posix_image_problems((image,)) == []

    def test_checks_every_directory_on_the_way_to_the_image(self, fake_stats):
        # The other half of B1's "or any directory on the path to it": a
        # root-owned, unwritable executable still isn't safe if the bundle
        # holding it can be deleted and replaced wholesale.
        image = Path("/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp")
        fake_stats(image, uid=0, gid=0, mode=0o755)
        fake_stats(Path("/Applications/PrivacyFenceApp.app"), uid=501, gid=20, mode=0o755)

        problems = privilege_separation._posix_image_problems((image,))

        assert len(problems) == 1
        assert "PrivacyFenceApp.app" in problems[0]

    def test_wired_into_audit_layout(self, separated, platform_name, monkeypatch, fake_stats):
        if platform_name == "win32":
            pytest.skip("Windows' layout audit is ACLs -- see TestWindowsLayoutAudit")
        monkeypatch.setattr(privilege_separation, "_authority_owner_problem", lambda _state: None)
        image = Path("/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp")
        monkeypatch.setattr(privilege_separation, "daemon_image_paths", lambda: (image,))
        fake_stats(image, uid=501, gid=20, mode=0o755)

        problems = privilege_separation.audit_layout()

        assert any("not root" in problem for problem in problems)

    def test_an_empty_daemon_image_paths_adds_nothing(self, separated, platform_name, monkeypatch):
        if platform_name == "win32":
            pytest.skip("Windows' layout audit is ACLs -- see TestWindowsLayoutAudit")
        # The ordinary case: an unfrozen (source/pip) install, or Linux's
        # .deb, where daemon_image_paths() is always empty.
        monkeypatch.setattr(privilege_separation, "_authority_owner_problem", lambda _state: None)
        monkeypatch.setattr(privilege_separation, "daemon_image_paths", lambda: ())

        assert privilege_separation.audit_layout() == []


class TestHandoffWrites:
    pytestmark = posix_permissions_only

    def test_write_handoff_file_is_ordinary_when_unseparated(self, monkeypatch, tmp_path):
        privilege_separation.reset_cache()
        target = tmp_path / "state" / "mcp_url"

        privilege_separation.write_handoff_file(target, "http://127.0.0.1:8765/mcp")

        assert target.read_text(encoding="utf-8") == "http://127.0.0.1:8765/mcp"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


class TestInstallerContract:
    """Each POSIX platform's shell script and this module are two halves of
    one contract: the script provisions a layout, the module resolves paths
    from it, and neither can see the other at runtime. A silent drift between
    them leaves a daemon looking for its data where the installer never put
    it.

    Everything here runs against both shell installers, because the two
    scripts share every constant except the three names -- and a check that
    only ever read one of them would not have caught the other drifting.

    Windows' installer has its own class below. It is PowerShell rather than
    bash, so not one of the ``NAME="value"`` reads here parses it, and it
    provisions ACLs rather than modes, so half of what is asserted here has
    nothing to compare against there."""

    SCRIPTS = {platform: INSTALLERS[platform].read_text(encoding="utf-8") for platform in POSIX_PLATFORMS}

    def _assign(self, platform: str, name: str) -> str:
        match = re.search(rf'^{name}="?([^"\n]*)"?$', self.SCRIPTS[platform], re.MULTILINE)
        assert match is not None, f"{name} is not assigned in {INSTALLERS[platform].name}"
        return match.group(1)

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_is_executable(self, platform):
        assert os.access(INSTALLERS[platform], os.X_OK)

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_account_names_match(self, platform):
        layout = privilege_separation.PLATFORM_LAYOUTS[platform]
        assert self._assign(platform, "SERVICE_ACCOUNT") == layout.service_account
        assert self._assign(platform, "SERVICE_GROUP") == layout.service_group

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_system_root_matches(self, platform):
        # as_posix(), not str(): this file is collected on Windows too, where
        # Path is a WindowsPath and stringifies the very same constant with
        # backslashes. The scripts' values are POSIX paths by nature, so that
        # spelling is the one both sides actually mean.
        expected = privilege_separation.PLATFORM_LAYOUTS[platform].system_root
        assert self._assign(platform, "SYSTEM_ROOT") == expected.as_posix()

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_the_module_names_this_installer(self, platform):
        # check_runtime_identity() tells a locked-out human which script to
        # run; a path that doesn't exist is worse than no path at all.
        installer = privilege_separation.PLATFORM_LAYOUTS[platform].installer
        assert (REPO_ROOT / installer).is_file()
        assert INSTALLERS[platform] == REPO_ROOT / installer

    def test_the_status_command_is_what_the_deb_puts_on_path(self):
        # Linux's is deliberately *not* the repo-relative script path: most
        # Linux installs are the .deb, which installs that same script under
        # a different name, and quoting a path someone doesn't have is worse
        # than quoting none. So the name in the error and the name the
        # package installs have to stay the same string.
        build_deb = (REPO_ROOT / "scripts" / "build_deb.sh").read_text(encoding="utf-8")
        command = privilege_separation.PLATFORM_LAYOUTS["linux"].status_command

        assert command == "sudo privacyfence-privilege-separation status"
        assert '/usr/sbin/privacyfence-privilege-separation"' in build_deb
        # And that what it installs there really is this platform's installer.
        assert "install -m 0755 scripts/linux_privilege_separation.sh" in build_deb

    def test_the_deb_ships_the_templates_that_script_renders(self):
        # The script resolves a checkout layout first and /usr/share second;
        # a packaged install has only the latter, so a template missing from
        # the .deb makes `enable` die at render_template() on exactly the
        # machines most likely to run it.
        build_deb = (REPO_ROOT / "scripts" / "build_deb.sh").read_text(encoding="utf-8")
        packaged_dir = re.search(
            r'^PACKAGED_TEMPLATE_DIR="([^"]+)"$', self.SCRIPTS["linux"], re.MULTILINE
        )
        assert packaged_dir is not None
        assert f'{packaged_dir.group(1)}/privacyfence-daemon.service.tmpl"' in build_deb
        assert f'{packaged_dir.group(1)}/privacyfence-companion.desktop.tmpl"' in build_deb

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_marker_matches(self, platform):
        assert self._assign(platform, "MARKER_NAME") == privilege_separation.MARKER_FILE_NAME
        assert int(self._assign(platform, "MARKER_VERSION")) == privilege_separation.MARKER_VERSION

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_writes_its_own_platform_into_the_marker(self, platform):
        # separation() rejects a marker whose platform isn't this one, so a
        # script writing the wrong string would produce an install that every
        # process reads as un-separated while a separated layout sits on disk
        # -- check_runtime_identity()'s worst case.
        assert f'"platform": "{platform}"' in self.SCRIPTS[platform]

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_handoff_dir_name_matches(self, platform):
        assert self._assign(platform, "HANDOFF_DIR_NAME") == privilege_separation.HANDOFF_DIR_NAME

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    @pytest.mark.parametrize(
        "script_name,module_constant",
        [
            ("SYSTEM_ROOT_MODE", privilege_separation.SYSTEM_ROOT_MODE),
            ("HANDOFF_DIR_MODE", privilege_separation.HANDOFF_DIR_MODE),
            ("AUTHORITY_DIR_MODE", privilege_separation.AUTHORITY_DIR_MODE),
            ("HANDOFF_FILE_MODE", privilege_separation.HANDOFF_FILE_MODE_SEPARATED),
        ],
    )
    def test_modes_match(self, platform, script_name, module_constant):
        # The scripts spell them as chmod arguments (711), the module as
        # Python octal literals (0o711) -- same numbers, two notations.
        assert int(self._assign(platform, script_name), 8) == module_constant

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_migrates_every_file_that_moved_into_the_handoff_dir(self, platform):
        # Each of these sits at the root of a pre-Phase-4 data directory and
        # has to end up inside handoff/, or something in the user's session
        # loses track of the daemon: the shim loses mcp_url/mcp_token, the
        # companion loses web_base_url.
        names = re.search(r"^HANDOFF_FILE_NAMES=\(([^)]*)\)$", self.SCRIPTS[platform], re.MULTILINE)
        assert names is not None
        moved = set(names.group(1).split())

        assert mcp_auth.MCP_TOKEN_FILE_NAME in moved
        assert control_channel.WEB_BASE_URL_FILE_NAME in moved
        from privacyfence.web import server

        assert server.MCP_URL_FILE_NAME in moved

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    @pytest.mark.parametrize("subcommand", ["enable", "disable", "status"])
    def test_documents_each_subcommand(self, platform, subcommand):
        assert f"cmd_{subcommand}()" in self.SCRIPTS[platform]

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_refuses_to_run_without_the_companion(self, platform):
        # ADR 0002 decision 2: after Phase 4 nobody in the user's desktop
        # session can mint a sign-in link except the companion, so installing
        # the daemon half alone is a locked door.
        assert "a separated install needs the companion app" in self.SCRIPTS[platform]

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_refuses_to_run_on_the_other_platform(self, platform):
        # Both scripts do the same chown/chmod/account work on paths that
        # exist under both OSes; running the wrong one would half-provision a
        # layout at a root nothing reads.
        assert "uname -s" in self.SCRIPTS[platform]

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_has_a_non_interactive_auto_mode(self, platform):
        # #428 D1 (4.1): both POSIX platforms' unattended auto-enable
        # trigger -- the .deb's postinst on Linux, the daemon's own
        # admin-prompt on macOS -- passes --auto, and it has to make enable
        # safe to run unattended (never die() a caller that can't recover
        # interactively) without silently skipping require_root. Windows
        # stays opt-in only -- D1 does not extend to it.
        assert "--auto" in self.SCRIPTS[platform]
        assert "AUTO=1" in self.SCRIPTS[platform]

    def test_the_debian_postinst_separates_on_configure(self):
        # The one place Linux's install-time separation actually gets invoked
        # from -- see debian/postinst's own comment for why postinst (already
        # root, at package-configure time) can safely do this where the daemon
        # itself couldn't.
        postinst = (REPO_ROOT / "debian" / "postinst").read_text(encoding="utf-8")
        assert "privacyfence-privilege-separation enable --machine-only" in postinst
        assert "privacyfence-privilege-separation enable --auto --for-user" in postinst
        assert '[ "$1" = "configure" ]' in postinst

    def test_the_debian_prerm_disables_on_remove(self):
        # #428 D1's auto-enable (above) needs an undo on the way out, or
        # `apt remove` stops privacyfence-privilege-separation --auto ever
        # wrote and strands a separated install's data (including live
        # connector OAuth tokens) under a directory the user can no longer
        # read, with the one tool that could reverse it just deleted.
        prerm = (REPO_ROOT / "debian" / "prerm").read_text(encoding="utf-8")
        assert "privacyfence-privilege-separation disable" in prerm
        remove_case = re.search(r"remove\)(.*?);;", prerm, re.DOTALL)
        assert remove_case is not None, "no `remove)` case in debian/prerm"
        assert "privacyfence-privilege-separation disable" in remove_case.group(1)
        assert "|| true" in remove_case.group(1)
        # Must not run on a mere upgrade -- that would tear down a running
        # separated install's unit mid-upgrade instead of leaving it alone.
        upgrade_case = re.search(r"\bupgrade\)(.*?);;", prerm, re.DOTALL)
        assert upgrade_case is not None, "no `upgrade)` case in debian/prerm"
        assert "privacyfence-privilege-separation" not in upgrade_case.group(1)

    def test_the_debian_prerm_stops_the_unit_on_upgrade(self):
        # dpkg unpacks the new version's files over /opt/privacyfence, where
        # a separated install's daemon runs its packaged PyInstaller onedir
        # build from, *before* postinst's
        # `enable --auto` gets a chance to stop and restart it -- a lazily
        # loaded shared library can vanish out from under the still-running
        # old process mid-upgrade. Stop the unit here first; postinst's
        # `enable --auto`, which already runs on every upgrade, starts it
        # again once the new files are in place.
        prerm = (REPO_ROOT / "debian" / "prerm").read_text(encoding="utf-8")
        upgrade_case = re.search(r"\bupgrade\)(.*?);;", prerm, re.DOTALL)
        assert upgrade_case is not None, "no `upgrade)` case in debian/prerm"
        assert "systemctl stop privacyfence-daemon.service" in upgrade_case.group(1)
        assert "|| true" in upgrade_case.group(1)
        # A bare `deconfigure` (no file swap happening) must stay a no-op.
        deconfigure_case = re.search(r"\bdeconfigure\)(.*?);;", prerm, re.DOTALL)
        assert deconfigure_case is not None, "no `deconfigure)` case in debian/prerm"
        assert deconfigure_case.group(1).strip() == ""


@pytest.mark.skipif(sys.platform == "win32", reason="runs the POSIX installers' own bash against a real unix socket")
class TestApplyLayoutLeavesSocketsAlone:
    """ADR 0029: ``apply_layout()`` runs on every ``enable``, including the
    re-run every package upgrade makes while a companion is still bound to
    ``handoff/companion.sock``. It used to ``chown -R``/``chmod -R`` the whole
    root, which handed that live socket to the service account and stripped
    its group bits -- after which the sticky ``handoff/`` stopped its own
    companion unlinking it and ADR 0027's owner check stopped every later
    companion binding at all.

    Runs each script's real ``apply_layout`` with ``chown``/``chmod`` stubbed
    on ``PATH`` to record their arguments, so no root is needed. "No ``-R``"
    plus "the regular file is named explicitly" is what tells the fixed
    ``find`` apart from the old recursion, which never named anything below
    the root and so would pass a bare "socket not in the arguments" check."""

    @staticmethod
    def _function(platform: str, name: str) -> str:
        text = INSTALLERS[platform].read_text(encoding="utf-8")
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
        assert match is not None, f"no {name}() in {INSTALLERS[platform].name}"
        return match.group(0)

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_re_owns_everything_but_a_live_socket(self, platform, tmp_path):
        # /tmp rather than tmp_path for the tree itself: macOS's sun_path is
        # 104 bytes, and pytest's tmp_path under /private/var/folders is
        # already most of that before a socket name is added.
        root = Path(tempfile.mkdtemp(prefix="pf-layout-", dir="/tmp"))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            handoff = root / "handoff"
            handoff.mkdir()
            token = handoff / "mcp_token"
            token.write_text("token", encoding="utf-8")
            companion_sock = handoff / "companion.sock"
            listener.bind(str(companion_sock))
            listener.listen(1)
            link = root / "points-outside"
            link.symlink_to(tmp_path)

            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log = tmp_path / "calls.log"
            for tool in ("chown", "chmod"):
                stub = bin_dir / tool
                stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "$CALL_LOG"\n', encoding="utf-8")
                stub.chmod(0o755)

            script = "\n".join([
                "set -euo pipefail",
                "note() { :; }",
                "move_handoff_files_in() { :; }",
                f"SYSTEM_ROOT={shlex.quote(str(root))}",
                "SERVICE_ACCOUNT=svc SERVICE_GROUP=grp HANDOFF_DIR_NAME=handoff",
                "SYSTEM_ROOT_MODE=711 AUTHORITY_DIR_MODE=700 HANDOFF_DIR_MODE=3770 HANDOFF_FILE_MODE=640",
                self._function(platform, "apply_layout"),
                "apply_layout",
            ])
            env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "CALL_LOG": str(log)}
            subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, timeout=30)

            calls = [line.split() for line in log.read_text(encoding="utf-8").splitlines()]
            chowns = [args[1:] for args in calls if args[0] == "chown"]
            chmods = [args[1:] for args in calls if args[0] == "chmod"]
            chowned = {arg for args in chowns for arg in args}
            chmodded = {arg for args in chmods for arg in args}

            assert all("-R" not in args for args in chowns + chmods), calls
            assert str(token) in chowned and str(token) in chmodded, calls
            assert str(root) in chowned, calls
            assert str(companion_sock) not in chowned | chmodded, calls
            # chown -h acts on the link itself; chmod has no such flag, so it
            # must not be handed the link at all.
            assert all(args[0] == "-h" for args in chowns), chowns
            assert str(link) not in chmodded, chmods
        finally:
            listener.close()
            shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="runs the macOS installer's own bash against a real unix socket")
class TestMacosScriptHelpers:
    """``macos_privilege_separation.sh`` helpers run for real under bash, with
    the macOS-only tools they call stubbed on ``PATH``: ``status`` reading
    ``handoff/``'s 3770 back as 770 (a real 4.2.1 install hit it), the
    daemon-owner check racing launchd's xpcproxy trampoline (v4.3.0), and
    ``uninstall [--purge]`` (ADR 0042) keeping or deleting the data."""

    _function = staticmethod(TestApplyLayoutLeavesSocketsAlone._function)

    def _run_uninstall(self, tmp_path, *, purge: bool, receipt: bool):
        """Runs the real ``cmd_uninstall``/``uninstall_services``/
        ``gui_session_uids`` against a scratch tree laid out like an installed,
        separated Mac. Every macOS tool it calls is a stub that logs its
        arguments; ``rm`` is the real one, confined to ``tmp_path`` by every
        path the script is given."""
        root = tmp_path / "Library" / "Application Support" / "PrivacyFence"
        (root / "authority" / "config").mkdir(parents=True)
        (root / "authority" / "config" / "settings.yaml").write_text("auto_accept: {}\n", encoding="utf-8")
        (root / "handoff").mkdir()
        (root / "handoff" / "mcp_token").write_text("t", encoding="utf-8")
        marker = root / "privilege-separation.json"
        marker.write_text('{\n  "owner_user": "alice"\n}\n', encoding="utf-8")
        image_parent = tmp_path / "Library" / "PrivacyFence"
        (image_parent / "image" / "PrivacyFenceApp.app").mkdir(parents=True)
        app = tmp_path / "Applications" / "PrivacyFenceApp.app"
        (app / "Contents").mkdir(parents=True)
        daemon_plist = tmp_path / "LaunchDaemons" / "com.privacyfence.daemon.plist"
        companion_plist = tmp_path / "LaunchAgents" / "com.privacyfence.companion.plist"
        for plist in (daemon_plist, companion_plist):
            plist.parent.mkdir()
            plist.write_text("<plist/>", encoding="utf-8")
        home = tmp_path / "Users" / "alice"
        home.mkdir(parents=True)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        stubs = {
            "launchctl": "exit 0",
            "dscl": "exit 0",
            # One GUI session (uid 501) and one process that is not one.
            "ps": "printf '  501 /System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow\\n'"
                  "; printf '  502 /usr/bin/some-daemon\\n'",
            "pkgutil": f'[ "$1" = --pkg-info ] && exit {0 if receipt else 1}; exit 0',
        }
        for tool, body in stubs.items():
            stub = bin_dir / tool
            stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "$CALL_LOG"\n{body}\n', encoding="utf-8")
            stub.chmod(0o755)

        script = "\n".join([
            "set -euo pipefail",
            "note() { :; }",
            "warn() { :; }",
            "require_macos() { :; }",
            "require_root() { :; }",
            "resolve_owner_optional() { :; }",
            "wait_for_daemon_unloaded() { :; }",
            "service_account_exists() { :; }",
            "service_group_exists() { :; }",
            f"PURGE={1 if purge else 0}",
            "OWNER_UID=501",
            "SERVICE_ACCOUNT=_privacyfence SERVICE_GROUP=_privacyfence",
            "DAEMON_LABEL=com.privacyfence.daemon COMPANION_LABEL=com.privacyfence.companion",
            f"SYSTEM_ROOT={shlex.quote(str(root))}",
            f"TRUSTED_IMAGE_DIR={shlex.quote(str(image_parent / 'image'))}",
            f"DEFAULT_APP={shlex.quote(str(app))}",
            f"DAEMON_PLIST={shlex.quote(str(daemon_plist))}",
            f"COMPANION_PLIST={shlex.quote(str(companion_plist))}",
            "PKG_ID=com.privacyfence.installer",
            self._function("darwin", "gui_session_uids"),
            self._function("darwin", "uninstall_services"),
            self._function("darwin", "cmd_uninstall"),
            "cmd_uninstall",
        ])
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "HOME": str(home),
        }
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return {
            "root": root, "marker": marker, "image_parent": image_parent, "app": app,
            "plists": (daemon_plist, companion_plist), "home": home, "calls": calls,
            "stdout": result.stdout,
        }

    def test_uninstall_stops_everything_and_keeps_the_data(self, tmp_path):
        r = self._run_uninstall(tmp_path, purge=False, receipt=True)

        assert "launchctl bootout system/com.privacyfence.daemon" in r["calls"]
        assert "launchctl bootout gui/501/com.privacyfence.companion" in r["calls"]
        # Only GUI sessions get a companion bootout, not every process's uid.
        assert not any("gui/502/" in call for call in r["calls"]), r["calls"]
        assert not any(plist.exists() for plist in r["plists"])
        assert not r["image_parent"].exists()
        assert not r["app"].exists()
        assert "pkgutil --forget com.privacyfence.installer" in r["calls"]

        # G3: the data, the marker and the account stay where they are...
        assert (r["root"] / "authority" / "config" / "settings.yaml").read_text(encoding="utf-8")
        assert (r["root"] / "handoff" / "mcp_token").exists()
        assert r["marker"].exists()
        assert not any(call.startswith("dscl . -delete") for call in r["calls"]), r["calls"]
        # ...and nothing lands in the owner's home directory.
        assert list(r["home"].iterdir()) == []
        assert "uninstall --purge" in r["stdout"]

    def test_uninstall_purge_also_deletes_the_data_and_the_account(self, tmp_path):
        r = self._run_uninstall(tmp_path, purge=True, receipt=True)

        assert not r["root"].exists()
        assert "dscl . -delete /Groups/_privacyfence" in r["calls"]
        assert "dscl . -delete /Users/_privacyfence" in r["calls"]
        assert not any(plist.exists() for plist in r["plists"])
        assert not r["image_parent"].exists()
        assert list(r["home"].iterdir()) == []

    def test_uninstall_leaves_an_app_the_pkg_did_not_install(self, tmp_path):
        # A source install separated with --daemon-exec/--companion-exec has no
        # receipt, and whatever sits at the default app path is not ours.
        r = self._run_uninstall(tmp_path, purge=False, receipt=False)

        assert r["app"].exists()
        assert not any(call.startswith("pkgutil --forget") for call in r["calls"]), r["calls"]
        assert not r["image_parent"].exists()

    def test_purge_is_refused_outside_uninstall(self):
        result = subprocess.run(
            ["bash", str(INSTALLERS["darwin"]), "status", "--purge"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert "--purge only applies to uninstall" in result.stderr

    def test_octal_mode_keeps_the_setgid_and_sticky_digit(self, tmp_path):
        handoff = tmp_path / "handoff"
        handoff.mkdir()
        handoff.chmod(0o3770)
        root = tmp_path / "root"
        root.mkdir()
        root.chmod(0o711)
        marker = tmp_path / "marker"
        marker.write_text("{}", encoding="utf-8")
        marker.chmod(0o644)

        env = dict(os.environ)
        if sys.platform != "darwin":
            # BSD `stat -f %Op` emulated with GNU stat's raw hex mode, so the
            # same helper runs here as on the macOS runner.
            gnu_stat = shutil.which("stat")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "stat"
            stub.write_text(f'#!/bin/sh\nprintf "%o\\n" "0x$({gnu_stat} -c %f "$3")"\n', encoding="utf-8")
            stub.chmod(0o755)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

        def mode(path):
            script = "\n".join([self._function("darwin", "octal_mode"), f"octal_mode {shlex.quote(str(path))}"])
            return subprocess.run(
                ["bash", "-c", script], env=env, check=True, capture_output=True, text=True, timeout=30,
            ).stdout.strip()

        try:
            assert mode(handoff) == "3770"
            assert mode(root) == "711"
            assert mode(marker) == "644"
        finally:
            handoff.chmod(0o700)

    def test_daemon_owner_waits_out_the_xpcproxy_trampoline(self, tmp_path):
        """launchd reports the job's pid while xpcproxy, running as root, is
        still in it -- before the switch to the plist's UserName and the exec
        of the app. The owner has to be read after that, or ``enable`` tears
        down a correctly started daemon as 'started as root' (v4.3.0's
        post-release build.yml runs)."""
        counter = tmp_path / "ps-calls"
        counter.write_text("0", encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ps = bin_dir / "ps"
        # The first three calls land in the trampoline, everything after it in
        # the app, whichever column is asked for -- so a read of the owner
        # that doesn't wait gets 'root'.
        ps.write_text(
            '#!/bin/sh\n'
            f'n=$(( $(cat {shlex.quote(str(counter))}) + 1 )); echo "$n" > {shlex.quote(str(counter))}\n'
            'case "$2" in\n'
            '  comm=) if [ "$n" -le 3 ]; then echo /usr/libexec/xpcproxy;'
            ' else echo /Library/PrivacyFence/image/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp; fi ;;\n'
            '  user=) if [ "$n" -le 3 ]; then echo root; else echo _privacyfence; fi ;;\n'
            'esac\n',
            encoding="utf-8",
        )
        ps.chmod(0o755)

        script = "\n".join([
            "set -euo pipefail",
            "DAEMON_PID_TIMEOUT=10",
            "daemon_pid() { echo 4242; }",
            self._function("darwin", "owner_of_pid"),
            self._function("darwin", "daemon_owner"),
            "daemon_owner",
        ])
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "_privacyfence"


class TestAutoEnableMacos:
    """#428 D1 (4.1) / ADR 0003 decision 6: the daemon's own trigger for
    auto-enabling privilege separation on macOS, since there's no
    package-manager postinst there to lean on the way Linux's .deb has.
    Nothing here can exercise the real ``osascript`` admin prompt (no macOS,
    no human to answer it) -- these cover the two things CI can prove: that
    the trigger fires (or correctly doesn't) under every precondition, and
    that the elevated command it builds is what it should be.

    Synchronous since decision 6: unlike the old #428 D1-only version, this
    is no longer backgrounded on a thread and no longer writes a one-shot
    marker -- ``enforce_separation()`` needs to read the outcome, and a
    decline is asked again on the next start rather than respected forever.
    """

    pytestmark = posix_permissions_only

    def test_noop_on_linux(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        privilege_separation.reset_cache()
        calls = []
        monkeypatch.setattr(privilege_separation, "_run_auto_enable_macos", lambda script: calls.append(script))

        privilege_separation.maybe_auto_enable_macos()

        assert calls == []

    def test_noop_when_already_separated(self, separated, monkeypatch):
        # separated is platform-parametrized (darwin and linux); running on
        # both proves the linux side is *also* a noop here, for the same
        # "already enabled" reason rather than the platform check above.
        calls = []
        monkeypatch.setattr(privilege_separation, "_run_auto_enable_macos", lambda script: calls.append(script))

        privilege_separation.maybe_auto_enable_macos()

        assert calls == []

    def test_noop_without_a_packaged_or_checkout_script(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()
        monkeypatch.setattr(privilege_separation, "_macos_installer_script_path", lambda: None)
        calls = []
        monkeypatch.setattr(privilege_separation, "_run_auto_enable_macos", lambda script: calls.append(script))

        privilege_separation.maybe_auto_enable_macos()

        assert calls == []

    def test_runs_the_elevated_attempt_synchronously(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()
        script = tmp_path / "macos_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(privilege_separation, "_macos_installer_script_path", lambda: script)
        # B2's script-safety check is covered by its own TestMacosAutoEnableScriptProblem
        # below; a script this test writes itself is never root-owned, so it is bypassed
        # here to keep this test about the dispatch behavior alone.
        monkeypatch.setattr(privilege_separation, "_macos_auto_enable_script_problem", lambda _script: None)
        calls = []
        monkeypatch.setattr(privilege_separation, "_run_auto_enable_macos", lambda s: calls.append(s))

        privilege_separation.maybe_auto_enable_macos()

        assert calls == [script]

        # Asked again on the very next call -- decision 6 retired the
        # one-shot marker that used to make a decline permanent.
        privilege_separation.maybe_auto_enable_macos()
        assert calls == [script, script]

    def test_applescript_quoting_escapes_quotes_and_backslashes(self):
        quoted = privilege_separation._applescript_quoted('a "quoted" \\path\\')
        # AppleScript's own escapes for a double-quoted string literal: a
        # literal backslash doubled, a literal quote backslash-escaped.
        assert quoted == '"a \\"quoted\\" \\\\path\\\\"'

    def test_run_auto_enable_builds_the_expected_elevated_command(self, monkeypatch, tmp_path):
        script = tmp_path / "a script with spaces.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        calls = []

        class _Result:
            returncode = 0
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _Result()

        monkeypatch.setattr(privilege_separation.subprocess, "run", fake_run)
        reset_calls = []
        monkeypatch.setattr(privilege_separation, "reset_cache", lambda: reset_calls.append(True))

        privilege_separation._run_auto_enable_macos(script)

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == privilege_separation._OSASCRIPT
        assert cmd[1] == "-e"
        applescript = cmd[2]
        assert applescript.startswith("do shell script ")
        assert applescript.endswith("with administrator privileges")
        assert str(script) in applescript
        assert "enable --auto" in applescript
        # The path has a space in it -- shlex.quote() must have wrapped that
        # argument in its own quoting before the AppleScript layer's quoting
        # went on top, or `sh -c` would see two words instead of one path.
        assert shlex.quote(str(script)) in applescript
        assert reset_calls == [True]

    def test_run_auto_enable_does_not_reset_cache_on_decline_or_failure(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")

        class _Result:
            returncode = 1
            stderr = "User canceled."

        monkeypatch.setattr(privilege_separation.subprocess, "run", lambda *a, **kw: _Result())
        reset_calls = []
        monkeypatch.setattr(privilege_separation, "reset_cache", lambda: reset_calls.append(True))

        privilege_separation._run_auto_enable_macos(script)

        assert reset_calls == []

    def test_run_auto_enable_swallows_a_subprocess_failure(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")

        def raising_run(*a, **kw):
            raise OSError("osascript not found")

        monkeypatch.setattr(privilege_separation.subprocess, "run", raising_run)

        # Must not raise -- this runs on a background thread with nothing to
        # catch an exception escaping it.
        privilege_separation._run_auto_enable_macos(script)

    def test_installer_script_path_prefers_the_app_bundle(self, monkeypatch, tmp_path):
        bundle = tmp_path / "PrivacyFenceApp.app"
        script_dir = bundle / "Contents" / "Resources" / "scripts"
        script_dir.mkdir(parents=True)
        script = script_dir / "macos_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(paths, "app_bundle_path", lambda: bundle)

        assert privilege_separation._macos_installer_script_path() == script

    def test_installer_script_path_falls_back_to_the_checkout(self, monkeypatch):
        monkeypatch.setattr(paths, "app_bundle_path", lambda: None)

        # This test itself runs from a checkout, so the real script is there.
        found = privilege_separation._macos_installer_script_path()
        assert found == REPO_ROOT / "scripts" / "macos_privilege_separation.sh"

    def test_installer_script_path_none_when_nothing_is_there(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "app_bundle_path", lambda: tmp_path / "Nothing.app")

        assert privilege_separation._macos_installer_script_path() is None

    def test_noop_when_the_resolved_script_fails_its_safety_check(self, monkeypatch, tmp_path):
        # #428 B2: whatever _macos_auto_enable_script_problem() decides (its
        # own logic is covered by TestMacosAutoEnableScriptProblem below) --
        # a script that fails it must produce no elevation prompt, just a
        # log line naming why.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()
        script = tmp_path / "macos_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(privilege_separation, "_macos_installer_script_path", lambda: script)
        monkeypatch.setattr(
            privilege_separation,
            "_macos_auto_enable_script_problem",
            lambda _script: f"{script} is owned by uid 501, not root",
        )
        calls = []
        monkeypatch.setattr(privilege_separation, "_run_auto_enable_macos", lambda s: calls.append(s))

        privilege_separation.maybe_auto_enable_macos()

        assert calls == []


class TestMacosAutoEnableScriptProblem:
    """#428 B2: the check ``maybe_auto_enable_macos()`` runs immediately
    before handing a script to ``osascript … with administrator
    privileges`` -- the fix for a new local privilege-escalation path D1
    introduced, where the elevated script was whatever the logged-in user
    (and therefore the agent) most recently put at the resolved path.

    CI cannot make a file root-owned, so ownership/mode are exercised
    through a faked ``os.stat()`` result rather than a real one -- the
    function only ever reads ``st_uid`` and ``st_mode`` off it."""

    pytestmark = posix_permissions_only

    @staticmethod
    def _fake_stat(st_uid: int, st_mode: int):
        return os.stat_result((st_mode, 0, 0, 1, st_uid, 0, 0, 0, 0, 0))

    def test_root_owned_and_unwritable_with_no_bundle_is_safe(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(
            privilege_separation.os, "stat", lambda _p: self._fake_stat(0, stat.S_IFREG | 0o755)
        )
        monkeypatch.setattr(paths, "app_bundle_path", lambda: None)

        assert privilege_separation._macos_auto_enable_script_problem(script) is None

    def test_not_root_owned_is_a_problem(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        monkeypatch.setattr(
            privilege_separation.os, "stat", lambda _p: self._fake_stat(501, stat.S_IFREG | 0o755)
        )

        problem = privilege_separation._macos_auto_enable_script_problem(script)

        assert problem is not None
        assert "not root" in problem

    def test_group_writable_is_a_problem(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        monkeypatch.setattr(
            privilege_separation.os, "stat", lambda _p: self._fake_stat(0, stat.S_IFREG | 0o775)
        )

        problem = privilege_separation._macos_auto_enable_script_problem(script)

        assert problem is not None
        assert "writable" in problem

    def test_world_writable_is_a_problem(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        monkeypatch.setattr(
            privilege_separation.os, "stat", lambda _p: self._fake_stat(0, stat.S_IFREG | 0o757)
        )

        problem = privilege_separation._macos_auto_enable_script_problem(script)

        assert problem is not None
        assert "writable" in problem

    def test_a_script_that_cannot_be_stat_ed_is_a_problem(self, monkeypatch, tmp_path):
        script = tmp_path / "does-not-exist.sh"

        problem = privilege_separation._macos_auto_enable_script_problem(script)

        assert problem is not None
        assert "stat" in problem

    def test_packaged_install_also_requires_the_bundle_signature_to_verify(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        bundle = tmp_path / "PrivacyFenceApp.app"
        monkeypatch.setattr(
            privilege_separation.os, "stat", lambda _p: self._fake_stat(0, stat.S_IFREG | 0o755)
        )
        monkeypatch.setattr(paths, "app_bundle_path", lambda: bundle)
        calls = []

        class _Result:
            returncode = 0
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _Result()

        monkeypatch.setattr(privilege_separation.subprocess, "run", fake_run)

        assert privilege_separation._macos_auto_enable_script_problem(script) is None
        assert calls == [[privilege_separation._CODESIGN, "--verify", "--deep", str(bundle)]]

    def test_a_bundle_whose_signature_does_not_verify_is_a_problem(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        bundle = tmp_path / "PrivacyFenceApp.app"
        monkeypatch.setattr(
            privilege_separation.os, "stat", lambda _p: self._fake_stat(0, stat.S_IFREG | 0o755)
        )
        monkeypatch.setattr(paths, "app_bundle_path", lambda: bundle)

        class _Result:
            returncode = 1
            stderr = "code object is not signed at all"

        monkeypatch.setattr(privilege_separation.subprocess, "run", lambda *a, **kw: _Result())

        problem = privilege_separation._macos_auto_enable_script_problem(script)

        assert problem is not None
        assert "signature" in problem

    def test_codesign_failing_to_run_at_all_is_a_problem_not_a_crash(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        bundle = tmp_path / "PrivacyFenceApp.app"
        monkeypatch.setattr(
            privilege_separation.os, "stat", lambda _p: self._fake_stat(0, stat.S_IFREG | 0o755)
        )
        monkeypatch.setattr(paths, "app_bundle_path", lambda: bundle)

        def raising_run(*a, **kw):
            raise OSError("codesign not found")

        monkeypatch.setattr(privilege_separation.subprocess, "run", raising_run)

        problem = privilege_separation._macos_auto_enable_script_problem(script)

        assert problem is not None
        assert "signature" in problem


class TestLaunchdTemplates:
    DAEMON = (MACOS_TEMPLATE_DIR / "com.privacyfence.daemon.plist.tmpl").read_text(encoding="utf-8")
    COMPANION = (MACOS_TEMPLATE_DIR / "com.privacyfence.companion.plist.tmpl").read_text(encoding="utf-8")

    def test_daemon_runs_as_the_service_account(self):
        # The one line that makes any of this true. A LaunchAgent runs as
        # whoever is logged in -- which is also who the agent runs as.
        assert "<key>UserName</key>" in self.DAEMON
        assert "__SERVICE_ACCOUNT__" in self.DAEMON
        assert "<key>GroupName</key>" in self.DAEMON
        assert "__SERVICE_GROUP__" in self.DAEMON

    def test_companion_names_no_account(self):
        # A LaunchAgent must *not* pin an account: it runs once per login
        # session, as whoever that session belongs to.
        assert "<key>UserName</key>" not in self.COMPANION

    def test_companion_is_limited_to_a_graphical_session(self):
        assert "LimitLoadToSessionType" in self.COMPANION
        assert "Aqua" in self.COMPANION

    @pytest.mark.parametrize("template_attr", ["DAEMON", "COMPANION"])
    def test_restarts_on_unexpected_exit(self, template_attr):
        # Same crash-restart contract the LaunchAgent had, and the one
        # tests/windows_task_contract.py holds the Windows task to.
        text = getattr(self, template_attr)
        assert "<key>KeepAlive</key>" in text
        assert "<key>SuccessfulExit</key>" in text

    @pytest.mark.parametrize("template_attr", ["DAEMON", "COMPANION"])
    def test_starts_at_load(self, template_attr):
        assert "<key>RunAtLoad</key>" in getattr(self, template_attr)

    def test_every_placeholder_is_one_the_installer_substitutes(self):
        substituted = set(re.findall(r"-e \"s\|(__[A-Z_]+__)\|", TestInstallerContract.SCRIPTS["darwin"]))
        used = set(re.findall(r"__[A-Z_]+__", self.DAEMON + self.COMPANION))

        assert used <= substituted, f"never substituted: {sorted(used - substituted)}"

    def test_labels_match_the_installer(self):
        script = TestInstallerContract.SCRIPTS["darwin"]
        assert re.search(r'^DAEMON_LABEL="com\.privacyfence\.daemon"$', script, re.MULTILINE)
        assert re.search(r'^COMPANION_LABEL="com\.privacyfence\.companion"$', script, re.MULTILINE)


class TestSystemdAndAutostartTemplates:
    """B5b's equivalent of TestLaunchdTemplates. The inversion is the same
    shape -- the daemon leaves the user's session, the companion enters it --
    but expressed as a system systemd unit plus an XDG autostart entry rather
    than a LaunchDaemon plus a LaunchAgent."""

    SCRIPT = TestInstallerContract.SCRIPTS["linux"]
    DAEMON = (LINUX_TEMPLATE_DIR / "privacyfence-daemon.service.tmpl").read_text(encoding="utf-8")
    COMPANION = (LINUX_TEMPLATE_DIR / "privacyfence-companion.desktop.tmpl").read_text(encoding="utf-8")

    def test_daemon_runs_as_the_service_account(self):
        # The one line that makes any of this true, in systemd's spelling: a
        # `--user` unit or an XDG autostart entry runs as whoever is logged in
        # -- which is also who the agent runs as -- and a system unit runs as
        # the account it names.
        assert "User=__SERVICE_ACCOUNT__" in self.DAEMON
        assert "Group=__SERVICE_GROUP__" in self.DAEMON

    def test_daemon_is_a_system_unit_not_a_user_one(self):
        # WantedBy=default.target is what the repo-root `--user` unit this
        # replaces uses; multi-user.target is the system-instance equivalent,
        # and installing the file to /etc/systemd/system is the other half.
        assert "WantedBy=multi-user.target" in self.DAEMON
        assert 'DAEMON_UNIT_PATH="/etc/systemd/system/${DAEMON_UNIT}"' in self.SCRIPT

    def test_daemon_restarts_on_unexpected_exit_only(self):
        # Same crash-restart contract as the LaunchDaemon's KeepAlive/
        # SuccessfulExit=false and the Windows task's RestartOnFailure:
        # Restart=always would fight the companion's own Quit action.
        assert "Restart=on-failure" in self.DAEMON
        assert "Restart=always" not in self.DAEMON

    def test_daemon_unit_does_not_isolate_tmp(self):
        # PrivateTmp= reads like free hardening and is a trap here: the one
        # /tmp use in this codebase is the control channel's own sun_path
        # overflow fallback (control_channel.socket_path_under()), and that
        # is a rendezvous between the daemon's account and the human's. A
        # private namespace would hide the daemon's socket from the
        # companion, presenting as "the companion can't reach the daemon".
        # The shipped root is short enough that the fallback should never
        # fire -- see test_a_real_installs_sockets_fit_in_sun_path -- but a
        # later hardening pass adding this line would make that assertion
        # the only thing standing between here and a silent breakage.
        directives = [
            line for line in self.DAEMON.splitlines() if line and not line.startswith(("#", "["))
        ]
        assert not [line for line in directives if line.startswith("PrivateTmp")]

    def test_daemon_umask_keeps_handoff_files_group_readable(self):
        # systemd's own default is 0022, which would strip nothing here -- but
        # 0007 is what makes the default agree with the explicit 0640 in
        # HANDOFF_FILE_MODE_SEPARATED instead of quietly widening a file that
        # slips through without one.
        assert "UMask=0007" in self.DAEMON

    def test_companion_autostart_names_no_account(self):
        # The XDG autostart entry is the LaunchAgent's counterpart: it runs
        # once per login session, as whoever that session belongs to. There is
        # no key that could pin an account, which is the point -- but it must
        # also not be handed the service account's own executable path.
        assert "__SERVICE_ACCOUNT__" not in self.COMPANION
        assert "__COMPANION_EXECUTABLE__" in self.COMPANION

    def test_companion_autostart_serves_the_control_channel(self):
        # --serve, not --action: a one-shot launcher would exit immediately
        # and leave the daemon's connector OAuth flows with no session-side
        # process to hand a browser URL to (ADR 0002 decision 5). Matched on
        # the Exec= line itself rather than the file, whose own comment
        # explains the distinction and so mentions both.
        exec_line = next(line for line in self.COMPANION.splitlines() if line.startswith("Exec="))
        assert exec_line.endswith("--serve")
        assert "--action" not in exec_line

    def test_companion_autostart_is_hidden_from_the_applications_menu(self):
        # The clickable menu entry is the *other* companion .desktop file
        # (resources/linux/privacyfence-companion.desktop); this one is
        # autostart-only, exactly like the daemon entry it replaces.
        assert "NoDisplay=true" in self.COMPANION

    def test_installer_moves_the_daemons_own_autostart_entry_aside(self):
        # Left in place it would start a second daemon as the logged-in user,
        # which on a separated install refuses to start (check_runtime_
        # identity) rather than quietly seeding a default policy.
        assert 'LEGACY_AUTOSTART_PATH="/etc/xdg/autostart/privacyfence.desktop"' in self.SCRIPT
        assert f'LEGACY_USER_UNIT="{Path("privacyfence.service").name}"' in self.SCRIPT
        assert "${LEGACY_AUTOSTART_PATH}.disabled" in self.SCRIPT

    def test_the_user_unit_it_disables_is_the_one_this_repo_ships(self):
        assert (REPO_ROOT / "privacyfence.service").is_file()

    def test_disabling_the_legacy_autostart_entry_also_hides_it(self):
        # B24: systemd-xdg-autostart-generator does not filter the autostart
        # directories by filename -- renaming the entry to *.desktop.disabled
        # alone does not stop it from being turned into a unit and started at
        # the next login. Hidden=true is the key the generator (and every
        # other XDG-autostart reader) actually honours, so hiding the entry
        # -- not just the rename -- has to be what stop_legacy_autostart does.
        assert "hide_autostart_entry" in self.SCRIPT
        assert 'hide_autostart_entry "$LEGACY_AUTOSTART_PATH"' in self.SCRIPT
        # The rename is unconditional in stop_legacy_autostart; hiding has to
        # run first so the file that lands at .disabled already carries it.
        hide_call = self.SCRIPT.index('hide_autostart_entry "$LEGACY_AUTOSTART_PATH"')
        rename_call = self.SCRIPT.index('mv "$LEGACY_AUTOSTART_PATH" "${LEGACY_AUTOSTART_PATH}.disabled"')
        assert hide_call < rename_call

    def test_an_already_disabled_but_unhidden_entry_is_healed(self):
        # An install separated by a script version that predates B24 has a
        # ${LEGACY_AUTOSTART_PATH}.disabled with no Hidden=true -- and
        # systemd has been autostarting it under its renamed name the whole
        # time. The postinst's machine half re-runs on every package upgrade
        # (debian/postinst's own `enable --machine-only` call), so
        # stop_legacy_autostart must heal that file in place rather than only
        # handling a fresh entry still at its original path.
        assert '"${LEGACY_AUTOSTART_PATH}.disabled" ] && ! autostart_entry_is_hidden' in self.SCRIPT

    def test_restoring_the_legacy_entry_undoes_the_hide(self):
        # `disable` puts the daemon's own autostart entry back in charge of
        # starting it in the logged-in user's session -- a Hidden=true this
        # script itself added would silently defeat that.
        assert "unhide_autostart_entry" in self.SCRIPT
        restore = self.SCRIPT[self.SCRIPT.index('restoring the daemon\'s XDG autostart entry'):]
        unhide_call = restore.index('unhide_autostart_entry "${LEGACY_AUTOSTART_PATH}.disabled"')
        rename_call = restore.index('mv "${LEGACY_AUTOSTART_PATH}.disabled" "$LEGACY_AUTOSTART_PATH"')
        assert unhide_call < rename_call

    def test_status_checks_the_disabled_entry_for_hidden_too(self):
        # Before B24's fix, `status` only ever looked at $LEGACY_AUTOSTART_
        # PATH -- which stop_legacy_autostart always renames away, so the
        # check reported no problem even when the renamed file was still
        # autostarting a second daemon. It has to also check the .disabled
        # file it actually left behind.
        assert (
            '"${LEGACY_AUTOSTART_PATH}.disabled" ] && ! autostart_entry_is_hidden "${LEGACY_AUTOSTART_PATH}.disabled"'
            in self.SCRIPT
        )
        assert self.SCRIPT.count('echo "  STILL AUTOSTARTS') == 2

    def test_every_placeholder_is_one_the_installer_substitutes(self):
        substituted = set(re.findall(r"-e \"s\|(__[A-Z_]+__)\|", self.SCRIPT))
        used = set(re.findall(r"__[A-Z_]+__", self.DAEMON + self.COMPANION))

        assert used <= substituted, f"never substituted: {sorted(used - substituted)}"

    def test_template_file_names_match_the_installer(self):
        assert re.search(r'^DAEMON_UNIT="privacyfence-daemon\.service"$', self.SCRIPT, re.MULTILINE)
        assert re.search(
            r'^COMPANION_AUTOSTART="privacyfence-companion\.desktop"$', self.SCRIPT, re.MULTILINE
        )
        # render_template() resolves "${TEMPLATE_DIR}/${NAME}.tmpl", so those
        # two names are also the two files on disk.
        assert (LINUX_TEMPLATE_DIR / "privacyfence-daemon.service.tmpl").is_file()
        assert (LINUX_TEMPLATE_DIR / "privacyfence-companion.desktop.tmpl").is_file()


class TestShimContract:
    """The fourth artifact. The MCPB shim resolves ``mcp_url``/``mcp_token``
    from the same marker, in its own TypeScript port of this module's
    discovery (``mcpb/shim/src/protocol.ts``) -- and a drift there is silent:
    the shim looks for the daemon in a directory no installer provisioned and
    reports "daemon not running" for a daemon that is running perfectly
    well."""

    SOURCE = SHIM_PROTOCOL.read_text(encoding="utf-8")

    def test_the_shim_knows_exactly_the_platforms_this_module_does(self):
        table = re.search(r"SYSTEM_ROOTS: Record<string, string> = \{(.*?)\};", self.SOURCE, re.DOTALL)
        assert table is not None, "SYSTEM_ROOTS is not a plain object literal in protocol.ts any more"
        roots = dict(re.findall(r'(\w+):\s*"([^"]+)"', table.group(1)))

        assert roots == {
            platform: layout.system_root.as_posix()
            for platform, layout in privilege_separation.PLATFORM_LAYOUTS.items()
        }

    def test_the_shim_reads_the_same_marker_file(self):
        assert privilege_separation.MARKER_FILE_NAME in self.SOURCE
        assert privilege_separation.HANDOFF_DIR_NAME in self.SOURCE

    def test_the_shim_honors_the_same_override(self):
        assert privilege_separation.SYSTEM_ROOT_ENV_VAR in self.SOURCE


class TestWindowsInstallerContract:
    """B5c's half of the same contract, against a PowerShell script instead
    of a shell one.

    Windows shares the marker, the directory names and the migration list
    with macOS and Linux, so those are asserted here exactly as
    ``TestInstallerContract`` asserts them for bash -- only the syntax of the
    assignment differs. What it does *not* share is a single mode: the layout
    is NTFS ACLs, and the checks for those are further down.
    """

    SCRIPT = INSTALLERS["win32"].read_text(encoding="utf-8")
    LAYOUT = privilege_separation.PLATFORM_LAYOUTS["win32"]

    def _assign(self, name: str) -> str:
        match = re.search(rf"^\${name} = '([^']*)'$", self.SCRIPT, re.MULTILINE)
        assert match is not None, f"${name} is not assigned in {INSTALLERS['win32'].name}"
        return match.group(1)

    def test_account_and_group_names_match(self):
        # The account is built from the service name in the script, the same
        # way the module builds it -- so this asserts the composed result
        # rather than a literal, which is the thing both halves have to agree
        # on for `sc create obj=` to name a real virtual account.
        assert self._assign("ServiceName") == privilege_separation.WINDOWS_SERVICE_NAME
        assert '$ServiceAccount = "NT SERVICE\\$ServiceName"' in self.SCRIPT
        assert self._assign("ServiceGroup") == self.LAYOUT.service_group

    def test_system_root_is_program_data(self):
        # The script never hardcodes C:, for the same reason system_root()
        # prefers the variable: a redirected %ProgramData% has to take both
        # halves with it, or the installer provisions one directory and every
        # process reads another.
        assert "$SystemRoot = Join-Path $env:ProgramData 'PrivacyFence'" in self.SCRIPT
        assert self.LAYOUT.system_root == Path("C:/ProgramData/PrivacyFence")

    def test_the_module_names_this_installer(self):
        assert (REPO_ROOT / self.LAYOUT.installer).is_file()
        assert INSTALLERS["win32"] == REPO_ROOT / self.LAYOUT.installer

    def test_the_status_command_names_what_the_installer_actually_puts_on_disk(self):
        # Deliberately not the repo-relative path, for the same reason
        # Linux's is not: almost every Windows install is the Inno one, which
        # copies this script next to the application under a different name,
        # and quoting a path someone does not have is worse than quoting
        # none. So the name in the error and the name the installer writes
        # have to stay the same string.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")

        assert 'DestName: "privilege-separation.ps1"' in inno
        assert r'..\scripts\windows_privilege_separation.ps1' in inno
        assert "privilege-separation.ps1" in self.LAYOUT.status_command
        assert "status" in self.LAYOUT.status_command

    def test_the_finish_page_mcpb_entries_run_as_the_current_user(self):
        # A real install hit "Internal error: CallSpawnServer: Unexpected
        # response: $0" on Finish-page click. `postinstall` alone defaults to
        # `runasoriginaluser`, and since PrivilegesRequired=admin means Setup
        # always runs elevated, that makes Setup spawn a helper under the
        # pre-UAC-prompt user's token to open the .mcpb (or, for the fallback
        # entry, explorer.exe) non-elevated -- a mechanism that failed
        # outright here instead of falling back, well after the real install
        # work (files, privilege separation, service, companion task) had
        # already succeeded. `runascurrentuser` skips that spawn and runs
        # with Setup's own already-elevated token instead.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")
        run_section = inno.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]

        mcpb_entries = [
            line for line in run_section.splitlines()
            if "Flags:" in line and "postinstall" in line
        ]
        assert len(mcpb_entries) == 2
        assert all("runascurrentuser" in line for line in mcpb_entries)

    def test_the_installer_ships_the_template_the_script_renders(self):
        # The script resolves a checkout layout first and its own directory
        # second; a real install has only the latter, so a template missing
        # from [Files] makes `enable` die at Install-CompanionTask on exactly
        # the machines most likely to run it.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")

        assert r'Source: "windows\privacyfence-companion-task.xml.tmpl"; DestDir: "{app}"' in inno
        assert (WINDOWS_TEMPLATE_DIR / "privacyfence-companion-task.xml.tmpl").is_file()
        assert "privacyfence-companion-task.xml.tmpl" in self.SCRIPT

    def test_the_installer_runs_enable_itself(self):
        # ADR 0003 decision 4, and the only part of it that is a contract
        # between two files rather than a behavior: Setup has to run *this*
        # script, by the name [Files] installs it under, with the machine
        # half's own subcommand.
        #
        # `enable` and not `enable -ForUser`: decision 3 split them so that
        # the half needing no human always runs, and -ForUser is the other
        # half on its own -- an installer calling it would refuse outright
        # against an install nothing has separated yet ("run '... enable'
        # first" in Invoke-EnableForUser).
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")

        separate = inno.split("function SeparateInstall", 1)[1].split("\nend;", 1)[0]

        assert "ScriptPath := ExpandConstant('{app}\\privilege-separation.ps1')" in separate
        assert "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " in separate
        assert "'\" enable > \"'" in separate
        assert "-ForUser" not in separate

    def test_a_failed_enable_fails_the_install(self):
        # The whole of decision 1 on this platform. Inno ignores a [Run]
        # entry's exit code and CurStepChanged's own autostart step
        # deliberately only warns, so "Setup finished successfully" is the
        # default outcome of anything that goes wrong in post-install --
        # which for this step would mean shipping an install whose approval
        # UI means less than it says. RaiseException is what turns it into a
        # rollback and a non-zero exit instead.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")
        post_install = inno.split("procedure CurStepChanged", 1)[1]

        assert "if not SeparateInstall(SeparationOutput) then" in post_install
        assert "RaiseException(" in post_install
        # ...and after the autostart registration, not before: `enable` ends
        # by disabling that task (Disable-DaemonTask), so registering it
        # afterwards would re-arm a second daemon in the user's own session
        # on every fresh install.
        assert post_install.index("RegisterAutostartTask()") < post_install.index("SeparateInstall(")

    def test_nothing_in_the_installer_starts_the_daemon_directly(self):
        # A separated install's daemon is a service; a copy of it started in
        # the logged-in user's session is refused by check_runtime_identity()
        # rather than merely redundant. So the Finish-page "launch now" entry
        # that used to run the alias exe is gone, and the only [Run] entries
        # left are the two that open the .mcpb.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")
        run_section = inno.split("\n[Run]\n", 1)[1].split("\n[UninstallRun]\n", 1)[0]
        # Comments in this section talk about the entry that was removed and
        # why, so they have to come out before asking what it still runs.
        entries = [line for line in run_section.splitlines() if not line.lstrip().startswith(";")]

        assert "{#AliasExeName}" not in "\n".join(entries)
        assert "{#AppExeName}" not in "\n".join(entries)

    def test_marker_matches(self):
        assert self._assign("MarkerName") == privilege_separation.MARKER_FILE_NAME
        assert f"$MarkerVersion = {privilege_separation.MARKER_VERSION}" in self.SCRIPT

    def test_writes_its_own_platform_into_the_marker(self):
        # separation() rejects a marker whose platform isn't this one, so a
        # script writing the wrong string would produce an install every
        # process reads as un-separated while a separated layout sits on disk
        # -- check_runtime_identity()'s worst case.
        assert "platform        = 'win32'" in self.SCRIPT

    def test_handoff_dir_name_matches(self):
        assert self._assign("HandoffDirName") == privilege_separation.HANDOFF_DIR_NAME

    def test_task_names_match_the_module_and_the_uninstaller(self):
        # Three places name these: the script that creates them, the module
        # that documents them, and the .iss whose [UninstallRun] has to
        # remove them from a machine that opted in and then uninstalled.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")

        assert self._assign("DaemonTaskName") == privilege_separation.WINDOWS_DAEMON_TASK_NAME
        assert self._assign("CompanionTaskName") == privilege_separation.WINDOWS_COMPANION_TASK_NAME
        assert f'#define CompanionTaskName "{privilege_separation.WINDOWS_COMPANION_TASK_NAME}"' in inno
        assert f'#define ServiceName "{privilege_separation.WINDOWS_SERVICE_NAME}"' in inno

    def test_the_uninstaller_removes_the_service_and_the_companion_task(self):
        # Both exist only on an install that opted in, and both outlive the
        # program files if nothing removes them -- a service whose binPath no
        # longer exists, and a task that fails at every sign-in.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")

        assert 'Parameters: "/delete /tn ""{#CompanionTaskName}"" /f"' in inno
        assert 'Parameters: "delete ""{#ServiceName}"""' in inno

    def test_migrates_every_file_that_moved_into_the_handoff_dir(self):
        names = re.search(r"^\$HandoffFileNames = @\(([^)]*)\)$", self.SCRIPT, re.MULTILINE)
        assert names is not None
        moved = {part.strip().strip("'") for part in names.group(1).split(",")}

        assert mcp_auth.MCP_TOKEN_FILE_NAME in moved
        assert control_channel.WEB_BASE_URL_FILE_NAME in moved
        from privacyfence.web import server

        assert server.MCP_URL_FILE_NAME in moved

    @pytest.mark.parametrize("subcommand", ["enable", "disable", "status"])
    def test_documents_each_subcommand(self, subcommand):
        assert f"function Invoke-{subcommand.capitalize()}" in self.SCRIPT
        assert f"'{subcommand}'" in self.SCRIPT

    def test_refuses_to_run_without_the_companion(self):
        # ADR 0002 decision 2, and on Windows it is stronger than on the
        # other two: a session-0 service cannot open a browser at all, so
        # without a companion even connector OAuth stops working.
        assert "a separated install needs the companion app" in self.SCRIPT

    def test_refuses_to_run_on_another_platform(self):
        assert "$env:OS -ne 'Windows_NT'" in self.SCRIPT

    def test_requires_administrator(self):
        # Creating a service and rewriting ACLs under %ProgramData% both need
        # it, and failing halfway through would leave a data directory nobody
        # owns.
        assert "WindowsBuiltInRole]::Administrator" in self.SCRIPT

    def test_refuses_a_user_writable_install(self):
        # #407, settled: a service runs whatever binPath names, so a
        # PrivacyFence the logged-in user can rewrite would hand the agent a
        # way to run its own code as the service account.
        assert "function Assert-ImageProtected" in self.SCRIPT
        assert "Assert-ImageProtected" in self.SCRIPT.split("function Invoke-Enable", 1)[1]

    def test_trustedinstaller_is_a_trusted_identity(self):
        # The regression a real Windows install hit: %ProgramFiles% is owned
        # by, and inherits a full-control grant to, NT SERVICE\TrustedInstaller
        # by default -- that is what makes %ProgramFiles% write-protected from
        # an ordinary Administrator token, not a gap in it. Without this SID
        # on Test-TrustedIdentity's list, Assert-ImageProtected refused every
        # install into the installer's own offered default location with
        # "... is writable by 'NT SERVICE\\TrustedInstaller'". Matches
        # windows_acl.TRUSTED_TRUSTEES, which the daemon's own startup audit
        # checks the same install against.
        test_trusted_identity = self.SCRIPT.split(
            "function Test-TrustedIdentity", 1,
        )[1].split("\nfunction ", 1)[0]

        assert "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464" in test_trusted_identity

    def test_takes_ownership_of_the_migrated_tree(self):
        # The hole a real platform-windows run exposed: Move-Data moves the
        # data directory out of %LOCALAPPDATA%, and a move preserves
        # ownership -- so without this the separated root is owned by the
        # human account being excluded, who can then rewrite every ACL below
        # it with no elevation at all.
        set_layout = self.SCRIPT.split("function Set-Layout", 1)[1].split("\nfunction ", 1)[0]

        assert "'/setowner'" in set_layout
        assert "$SidAdministrators" in set_layout
        # And verified rather than hoped for: /c lets icacls continue past an
        # entry it cannot rewrite, so a silent failure would leave a layout
        # that looks right in every other respect.
        assert "could not take ownership" in set_layout

    def test_disable_hands_ownership_back(self):
        # Through Restore-LegacyDataDir, which Invoke-Disable and the rollback
        # in Invoke-Enable share -- an `enable` that fails midway owes the
        # human exactly what `disable` does.
        disable = self.SCRIPT.split("function Invoke-Disable", 1)[1].split("\nfunction ", 1)[0]
        restore = self.SCRIPT.split("function Restore-LegacyDataDir", 1)[1].split("\nfunction ", 1)[0]

        assert "Restore-LegacyDataDir" in disable
        assert "'/setowner', $script:OwnerUser" in restore

    def test_a_failed_enable_leaves_neither_half_of_a_move_behind(self):
        # privacyfence/privacyfence#599: the observed failure left the daemon's
        # data under %ProgramData% with no marker, no service and no companion
        # task pointing at it -- a layout paths.py resolves for nobody. Two
        # things stop that now, and this asserts both: `sc create` (the step
        # that failed) happens before the data is moved at all, and everything
        # from there on is inside a catch that walks the move back.
        enable = self.SCRIPT.split("function Invoke-Enable", 1)[1].split("\nfunction ", 1)[0]
        # Call sites only. A comment that names a later step to explain an
        # earlier one is ordinary and correct in this script, and indexing
        # the raw text made it read as the step itself having moved.
        calls = "\n".join(
            line for line in enable.splitlines() if not line.strip().startswith("#")
        )

        assert calls.index("Install-DaemonService") < calls.index("Move-Data")
        assert "Undo-PartialEnable" in enable
        for step in ("Move-Data", "Set-Layout", "Write-Marker", "Install-CompanionTask"):
            assert step in enable.split("try {", 1)[1].split("} catch {", 1)[0], step

    def test_the_rollback_cannot_replace_the_failure_it_is_reporting(self):
        # It runs inside a catch whose exception is about to be re-thrown, and
        # that exception is the only account of why `enable` failed. A rollback
        # that threw its own would lose it.
        undo = self.SCRIPT.split("function Undo-PartialEnable", 1)[1].split("\nfunction ", 1)[0]

        assert undo.count("try {") == 1 and "} catch {" in undo
        assert "Restore-LegacyDataDir" in undo
        assert "Enable-DaemonTask" in undo

    def test_status_checks_the_owner(self):
        assert "WRONG OWNER" in self.SCRIPT

    def test_severs_inheritance_before_granting_anything(self):
        # The single most load-bearing line in the script: %ProgramData%
        # grants Users read-and-execute by inheritance, so a directory
        # created under it is readable by every account on the machine until
        # that inheritance is cut.
        assert self.SCRIPT.count("'/inheritance:r'") >= 4

    def test_grants_the_root_traverse_but_not_listing(self):
        # POSIX 0711, in the primitive Windows has. (X) is execute/traverse
        # with no FILE_READ_DATA, and it carries no (OI)(CI), so it does not
        # reach authority\ or handoff\ either.
        assert '"${SidUsers}:(X)"' in self.SCRIPT

    def test_the_marker_stays_readable_from_the_users_own_session(self):
        # The root grants Users traverse only, so without an ACE of its own
        # the one file that tells a user-session process the layout moved
        # would be the one file it cannot read -- and paths.py would resolve
        # the unseparated directory for the companion and the agent.
        assert '"${SidUsers}:(R)"' in self.SCRIPT

    def test_uses_well_known_sids_for_built_in_principals(self):
        # "BUILTIN\\Users" is "BUILTIN\\Utilisateurs" on a French Windows and
        # icacls would reject it; the SID is the same string everywhere.
        assert "$SidSystem = '*S-1-5-18'" in self.SCRIPT
        assert "$SidAdministrators = '*S-1-5-32-544'" in self.SCRIPT
        assert "$SidUsers = '*S-1-5-32-545'" in self.SCRIPT


class TestWindowsCompanionTaskTemplate:
    """ADR 0002's "startup wiring inverts", on Windows: what autostarts in
    the user's session stops being the daemon and becomes the companion."""

    TEMPLATE = (WINDOWS_TEMPLATE_DIR / "privacyfence-companion-task.xml.tmpl").read_text(encoding="utf-8")

    def test_matches_the_shared_task_contract(self):
        from tests.windows_task_contract import (
            EXEC_PATH_PLACEHOLDER,
            assert_task_xml_matches_companion_contract,
        )

        assert_task_xml_matches_companion_contract(self.TEMPLATE, exec_path=EXEC_PATH_PLACEHOLDER)

    def test_runs_the_companion_not_the_daemon(self):
        script = INSTALLERS["win32"].read_text(encoding="utf-8")

        assert "$DefaultCompanionExecName = 'PrivacyFenceCompanion.exe'" in script
        # And that the placeholder the script substitutes is the one the
        # template actually carries.
        assert "__EXEC_PATH__" in self.TEMPLATE
        assert "'__EXEC_PATH__'" in script

    def test_the_installer_disables_the_daemons_own_task(self):
        # Left enabled it would start a second daemon in the logged-in user's
        # session at every sign-in, which on a separated install refuses to
        # start (check_runtime_identity) rather than quietly seeding a
        # default policy -- loud, but still a daemon that is not running for
        # the reason the log says.
        script = INSTALLERS["win32"].read_text(encoding="utf-8")

        assert "Disable-ScheduledTask -TaskName $DaemonTaskName" in script
        assert "Enable-ScheduledTask -TaskName $DaemonTaskName" in script

    def test_the_companion_is_reachable_by_hand_as_well(self):
        # The companion has no crash-restart of its own (see the template's
        # own comment on why a repeating trigger is wrong here), so the Start
        # Menu shortcut is the recovery path when the tray icon is gone.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")

        assert '#define CompanionExeName "PrivacyFenceCompanion.exe"' in inno
        assert r'Name: "{group}\{#AppName} Companion"' in inno

    def test_the_main_start_menu_entry_launches_through_the_companion(self):
        # ADR 0031: the same thing a double-click on the macOS app does --
        # open Approvals, starting the tray first if it isn't running --
        # rather than the bare settings URL, which only worked for a browser
        # that already had a session.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")

        assert (
            r'Name: "{group}\{#AppName}"; Filename: "{app}\{#CompanionExeName}"; Parameters: "--launch"'
            in inno
        )
        assert "localhost:8765" not in inno


class TestMacosBundleMainExecutable:
    """ADR 0031: a double-click on PrivacyFenceApp.app runs the launcher,
    never the daemon. launchd starts the daemon and the companion by their
    own explicit paths (macos_privilege_separation.sh), so the bundle's
    CFBundleExecutable is free to be something a human means to click."""

    SPEC = REPO_ROOT / "PrivacyFenceApp.spec"

    def test_the_bundles_main_executable_is_the_launcher(self):
        spec = self.SPEC.read_text(encoding="utf-8")

        assert '"CFBundleExecutable": "PrivacyFence",' in spec
        assert '["src/_launcher_entry.py"]' in spec
        assert 'name="PrivacyFence",' in spec

    def test_the_launcher_runs_the_companions_launch(self):
        entry = (REPO_ROOT / "src" / "_launcher_entry.py").read_text(encoding="utf-8")

        assert 'main(["--launch"])' in entry

    def test_launchd_still_starts_the_daemon_by_its_own_path(self):
        script = (REPO_ROOT / "scripts" / "macos_privilege_separation.sh").read_text(encoding="utf-8")

        assert 'DAEMON_EXECUTABLE="${staged_app}/Contents/MacOS/PrivacyFenceApp"' in script
        assert 'COMPANION_EXECUTABLE="${staged_app}/Contents/MacOS/PrivacyFenceCompanion"' in script


class TestWindowsLayoutAudit:
    """``audit_layout()``'s Windows branch, driven through a synthetic DACL.

    Nothing here needs Windows: ``windows_acl.read_dacl()`` is the only part
    that does, and it is exactly one call this replaces. What that buys is
    that the *decisions* -- which ACE is a defect and which is the design --
    are asserted on every PR by the ordinary Ubuntu suite, rather than only
    by the Windows job and only for whatever ACL that runner happens to have.
    ``tests/platform/test_windows_acls.py`` covers the other half: that a
    directory icacls really has provisioned reads back the way this expects.
    """

    ACCOUNT = privilege_separation.WINDOWS_SERVICE_ACCOUNT_NAME
    GROUP = privilege_separation.WINDOWS_SERVICE_GROUP_NAME

    @pytest.fixture
    def separated_windows(self, monkeypatch, tmp_path):
        root = tmp_path / "PrivacyFence"
        (root / "authority").mkdir(parents=True)
        (root / privilege_separation.HANDOFF_DIR_NAME).mkdir()
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
        _write_marker(root, "win32")
        yield root
        privilege_separation.reset_cache()

    def _install_dacls(self, monkeypatch, root: Path, overrides: dict[Path, list] | None = None):
        """What the installer provisions, per directory, with any one of them
        replaced by ``overrides``. Modelling all three at once matters: most
        of the failure modes here are a *missing* severance of inheritance on
        one directory while the other two are fine."""
        full = windows_acl.FILE_ALL_ACCESS
        provisioned = {
            root: [
                windows_acl.Ace(self.ACCOUNT, full),
                windows_acl.Ace("NT AUTHORITY\\SYSTEM", full),
                windows_acl.Ace("BUILTIN\\Administrators", full),
                windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_TRAVERSE_ONLY),
            ],
            root / "authority": [
                windows_acl.Ace(self.ACCOUNT, full),
                windows_acl.Ace("NT AUTHORITY\\SYSTEM", full),
            ],
            root / privilege_separation.HANDOFF_DIR_NAME: [
                windows_acl.Ace(self.ACCOUNT, full),
                windows_acl.Ace(f"MACHINE\\{self.GROUP}", windows_acl.FILE_GENERIC_READ_EXECUTE),
            ],
        }
        provisioned.update(overrides or {})
        monkeypatch.setattr(windows_acl, "read_dacl", lambda path: provisioned.get(Path(path)))
        monkeypatch.setattr(windows_acl, "has_null_dacl", lambda _path: False)

    def test_a_provisioned_layout_reports_nothing(self, separated_windows, monkeypatch):
        self._install_dacls(monkeypatch, separated_windows)

        assert privilege_separation.audit_layout() == []

    def test_reports_a_root_that_can_be_enumerated(self, separated_windows, monkeypatch):
        # The inheritance %ProgramData% hands out for free, left in place:
        # every account on the machine can list the data directory.
        self._install_dacls(monkeypatch, separated_windows, {
            separated_windows: [
                windows_acl.Ace(self.ACCOUNT, windows_acl.FILE_ALL_ACCESS),
                windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_GENERIC_READ_EXECUTE, inherited=True),
            ],
        })

        problems = privilege_separation.audit_layout()

        assert any("list its contents" in problem for problem in problems)

    def test_reports_an_authority_dir_the_user_can_still_reach(self, separated_windows, monkeypatch):
        # The Windows counterpart of "0700, but under the human's own uid":
        # the mode looks right and the boundary is not there.
        self._install_dacls(monkeypatch, separated_windows, {
            separated_windows / "authority": [
                windows_acl.Ace(self.ACCOUNT, windows_acl.FILE_ALL_ACCESS),
                windows_acl.Ace("MACHINE\\alice", windows_acl.FILE_GENERIC_READ_EXECUTE),
            ],
        })

        problems = privilege_separation.audit_layout()

        assert any("not actually in effect" in problem for problem in problems)

    def test_a_deny_ace_is_not_a_grant(self, separated_windows, monkeypatch):
        self._install_dacls(monkeypatch, separated_windows, {
            separated_windows / "authority": [
                windows_acl.Ace(self.ACCOUNT, windows_acl.FILE_ALL_ACCESS),
                windows_acl.Ace("MACHINE\\alice", windows_acl.FILE_ALL_ACCESS, allowed=False),
            ],
        })

        assert privilege_separation.audit_layout() == []

    def test_reports_a_handoff_dir_the_group_cannot_read(self, separated_windows, monkeypatch):
        # The failure that presents as "the daemon is not running": the shim
        # cannot read mcp_token, so it reports no daemon against one that is
        # running perfectly well.
        self._install_dacls(monkeypatch, separated_windows, {
            separated_windows / privilege_separation.HANDOFF_DIR_NAME: [
                windows_acl.Ace(self.ACCOUNT, windows_acl.FILE_ALL_ACCESS),
            ],
        })

        problems = privilege_separation.audit_layout()

        assert any("cannot reach mcp_token" in problem for problem in problems)

    def test_reports_a_handoff_dir_the_group_can_write(self, separated_windows, monkeypatch):
        # Nothing in the user's session creates anything there on Windows --
        # both channels are named pipes -- so a writable handoff would only
        # let the agent replace the discovery files the companion reads.
        self._install_dacls(monkeypatch, separated_windows, {
            separated_windows / privilege_separation.HANDOFF_DIR_NAME: [
                windows_acl.Ace(self.ACCOUNT, windows_acl.FILE_ALL_ACCESS),
                windows_acl.Ace(f"MACHINE\\{self.GROUP}", windows_acl.FILE_ALL_ACCESS),
            ],
        })

        problems = privilege_separation.audit_layout()

        assert any("never to rewrite it" in problem for problem in problems)

    def test_a_directory_it_cannot_read_is_skipped_rather_than_reported(
        self, separated_windows, monkeypatch,
    ):
        # Same best-effort posture as the POSIX branch: the process running
        # the audit may legitimately not be able to see a path -- that is
        # half the point of the layout.
        self._install_dacls(monkeypatch, separated_windows, {separated_windows / "authority": None})
        monkeypatch.setattr(windows_acl, "read_dacl", lambda path: None)

        assert privilege_separation.audit_layout() == []

    def test_a_null_dacl_is_reported_rather_than_read_as_unknown(
        self, separated_windows, monkeypatch,
    ):
        # The one case read_dacl() cannot express: no DACL at all grants
        # every account full control, which is the opposite of the empty list
        # it would otherwise look like.
        monkeypatch.setattr(windows_acl, "read_dacl", lambda _path: None)
        monkeypatch.setattr(windows_acl, "has_null_dacl", lambda _path: True)

        problems = privilege_separation.audit_layout()

        assert len(problems) == 3
        assert all("no access-control list at all" in problem for problem in problems)

    def test_reports_a_root_the_human_still_owns(self, separated_windows, monkeypatch):
        # The Windows counterpart of "authority/ is 0700 under the human's
        # own uid", and sharper: an owner holds WRITE_DAC implicitly, so a
        # perfect ACL under the wrong owner is one command from being undone.
        # `enable` *moves* the data directory out of %LOCALAPPDATA%, and a
        # move preserves ownership -- so this is the default outcome without
        # an explicit /setowner, not an exotic one.
        self._install_dacls(monkeypatch, separated_windows)
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "MACHINE\\alice")

        problems = privilege_separation.audit_layout()

        assert len(problems) == 3
        assert all("rewrite its access-control list" in problem for problem in problems)

    def test_an_administrator_owned_layout_is_clean(self, separated_windows, monkeypatch):
        self._install_dacls(monkeypatch, separated_windows)
        monkeypatch.setattr(
            windows_acl, "read_owner", lambda _path: "BUILTIN\\Administrators"
        )

        assert privilege_separation.audit_layout() == []

    def test_an_owner_rights_ace_is_resolved_against_that_owner(
        self, separated_windows, monkeypatch,
    ):
        # The whole reason audit_layout() reads the owner before the DACL
        # rather than passing each check a bare ACE list -- found by a real
        # platform-windows run, where an OWNER RIGHTS ACE Windows had put on
        # the directory failed a correctly provisioned root.
        owner_rights = windows_acl.Ace(
            windows_acl.OWNER_RIGHTS_TRUSTEE, windows_acl.FILE_ALL_ACCESS
        )
        self._install_dacls(monkeypatch, separated_windows, {
            separated_windows: [
                windows_acl.Ace(self.ACCOUNT, windows_acl.FILE_ALL_ACCESS),
                owner_rights,
            ],
        })
        monkeypatch.setattr(
            windows_acl, "read_owner", lambda _path: "BUILTIN\\Administrators"
        )

        assert privilege_separation.audit_layout() == []

    def test_checks_the_daemons_own_image_when_frozen(self, separated_windows, monkeypatch, tmp_path):
        # The check with no POSIX counterpart. Both the executable and the
        # directory holding it: one is "replace the binary", the other is
        # "drop a DLL next to it".
        image = tmp_path / "Program Files" / "PrivacyFence" / "privacyfence-app.exe"
        image.parent.mkdir(parents=True)
        image.write_text("", encoding="utf-8")
        monkeypatch.setattr(privilege_separation, "daemon_image_paths", lambda: (image, image.parent))
        user_writable = [windows_acl.Ace("MACHINE\\alice", windows_acl.FILE_ALL_ACCESS)]
        self._install_dacls(monkeypatch, separated_windows, {
            image: user_writable,
            image.parent: user_writable,
        })

        problems = privilege_separation.audit_layout()

        assert len(problems) == 2
        assert all("can run code as that account" in problem for problem in problems)

    def test_a_frozen_install_checks_the_exe_and_the_directory_holding_it(self, monkeypatch):
        # Two paths rather than one: rewriting the executable and dropping a
        # DLL beside it are both "the service runs code the user chose", and
        # only the second is stopped by the exe's own ACL.
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", "/tmp/_MEI", raising=False)
        monkeypatch.setattr(sys, "executable", "/opt/pf/privacyfence-app.exe")

        assert privilege_separation.daemon_image_paths() == (
            Path("/opt/pf/privacyfence-app.exe"),
            Path("/opt/pf"),
        )

    def test_an_unfrozen_install_has_no_image_to_check(self, monkeypatch):
        # sys.executable is the Python interpreter for a source or pip
        # install -- shared with every other Python program on the machine,
        # and not something this install provisioned or can speak about.
        monkeypatch.delattr(sys, "frozen", raising=False)

        assert privilege_separation.daemon_image_paths() == ()


class TestWindowsServiceHost:
    """The service is B5c's one genuinely new moving part: macOS and Linux
    pointed a different service manager at the same unchanged executable,
    and Windows cannot, because its SCM waits to be called back."""

    def test_the_daemon_entry_point_routes_the_service_flag(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "privacyfence.windows_service.run_service", lambda: called.append(True) or 0
        )

        assert daemon_main.main(["--windows-service"]) == 0
        assert called == [True]

    def test_the_service_flag_short_circuits_before_any_config_is_read(self, monkeypatch):
        # It has to: this process is the dispatcher, not the daemon. The SCM
        # calls back into main() with no arguments, and every startup check
        # -- check_runtime_identity() included -- runs there, exactly once.
        monkeypatch.setattr("privacyfence.windows_service.run_service", lambda: 0)
        monkeypatch.setattr(
            daemon_main, "load_config", lambda *_args, **_kwargs: pytest.fail("config was read")
        )
        monkeypatch.setattr(
            privilege_separation, "check_runtime_identity",
            lambda: pytest.fail("the dispatcher ran the daemon's own startup checks"),
        )

        assert daemon_main.main(["--windows-service"]) == 0

    def test_the_service_name_and_the_account_are_one_fact(self):
        # Windows derives a virtual account's name from its service's, so a
        # service host naming a different service would produce an account no
        # ACL on disk mentions.
        assert windows_service.WINDOWS_SERVICE_NAME == privilege_separation.WINDOWS_SERVICE_NAME
        assert privilege_separation.WINDOWS_SERVICE_ACCOUNT_NAME.endswith(
            windows_service.WINDOWS_SERVICE_NAME
        )

    def test_the_installer_registers_exactly_that_flag(self):
        # The one string that connects the two: the SCM starts what binPath
        # says, and anything else would be killed after 30 seconds with error
        # 1053 for never calling StartServiceCtrlDispatcher.
        script = INSTALLERS["win32"].read_text(encoding="utf-8")

        assert "--windows-service" in script
        assert "'obj=', (ConvertTo-CommandLineToken $ServiceAccount)" in script

    def test_the_service_is_created_from_a_command_line_this_script_builds(self):
        # `sc create`'s binPath= value is `"<path>" --windows-service`: one
        # argument, with quotes inside it, because the SCM runs ImagePath as
        # a command line and an unquoted `C:\Program Files\...` there is the
        # unquoted-service-path hijack. Windows PowerShell 5.1's native
        # argument binder cannot carry such an argument -- it wraps the whole
        # thing in another pair of quotes without escaping the inner ones, so
        # sc.exe receives `C:\Program` as the binPath and
        # `Files\PrivacyFence\privacyfence-app.exe --windows-service` as an
        # option it has never heard of, and answers exit 1639. That is why
        # this one call builds its own command line instead of handing
        # Invoke-Sc an array, and why nothing may quietly hand it back.
        script = INSTALLERS["win32"].read_text(encoding="utf-8")

        create = re.search(
            r"Invoke-NativeCommandLine -FilePath 'sc\.exe' -CommandLine \(@\((.*?)\) -join ' '\)",
            script,
            re.DOTALL,
        )
        assert create is not None, (
            "`sc create` no longer goes through Invoke-NativeCommandLine -- an argument array "
            "cannot carry binPath='s embedded quotes under Windows PowerShell 5.1"
        )
        assert "'create'" in create.group(1)
        assert "ConvertTo-CommandLineToken" in create.group(1)
        # Invoke-Sc, the array-based path, must not be the thing that creates
        # the service again.
        assert "Invoke-Sc @(\n        'create'" not in script

    def test_the_command_line_quoter_escapes_embedded_quotes(self):
        # The rule CommandLineToArgvW parses back: a quote inside a token is
        # \", and the backslashes immediately before a quote (or at the end of
        # the token) double so they stay literal instead of escaping it.
        script = INSTALLERS["win32"].read_text(encoding="utf-8")

        assert "function ConvertTo-CommandLineToken" in script
        assert r"""[regex]::Replace($Value, '(\\*)"', '$1$1\"')""" in script
        assert r"""[regex]::Replace($escaped, '(\\+)$', '$1$1')""" in script

    def test_the_command_line_runner_drains_both_pipes(self):
        # A child filling one pipe while this waits on the other never exits,
        # and sc.exe's usage dump on a bad command line -- the output the
        # error message exists to carry -- is exactly that kind of output.
        script = INSTALLERS["win32"].read_text(encoding="utf-8")

        assert "$process.StandardOutput.ReadToEndAsync()" in script
        assert "$process.StandardError.ReadToEndAsync()" in script
        assert script.index("ReadToEndAsync") < script.index("$process.WaitForExit()")

    def test_the_module_imports_without_pywin32(self):
        # Deliberate: the ServiceFramework subclass is built inside a
        # function, so importing this module (from a test, or from
        # daemon_main's own argument routing) never requires Windows.
        assert windows_service.SERVICE_DISPLAY_NAME
        assert "#428" in windows_service.SERVICE_DESCRIPTION


class TestWindowsChannelTrustees:
    """``socket_mode()``'s ``0660``, in the primitive Windows has: a named
    pipe's DACL names the account on the other end rather than a group bit
    opening up a socket node."""

    def test_empty_on_an_unseparated_install(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        privilege_separation.reset_cache()

        assert privilege_separation.windows_channel_trustees() == ()

    def test_names_both_principals_on_a_separated_one(self, monkeypatch, tmp_path):
        # Both, not just the group: a virtual service account holds no group
        # memberships, so granting the group alone would leave the daemon's
        # own end of the companion's pipe unreachable.
        root = tmp_path / "PrivacyFence"
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
        _write_marker(root, "win32")

        assert privilege_separation.windows_channel_trustees() == (
            privilege_separation.WINDOWS_SERVICE_ACCOUNT_NAME,
            privilege_separation.WINDOWS_SERVICE_GROUP_NAME,
        )

    def test_empty_on_a_separated_posix_install(self, separated, platform_name):
        # POSIX draws this with socket_mode()'s 0660 and the shared group;
        # there is no pipe DACL to add anyone to.
        if platform_name == "win32":
            pytest.skip("this is the POSIX branch")

        assert privilege_separation.windows_channel_trustees() == ()


class TestMachineHalfMarker:
    """ADR 0003 decision 3's marker compatibility, which is the trap in that
    phase: the machine half has to record *something* for ``owner_user``, and
    ``_parse_marker()`` refuses a marker missing the key outright while
    ``check_runtime_identity()`` turns a refused marker into a startup
    failure by design. So it records ``""`` and ``MARKER_VERSION`` stays at
    1 -- bumping it would make a 4.1 daemon, or any rollback, refuse to start
    against a 4.2-provisioned install."""

    @pytest.fixture(autouse=True)
    def _under_tmp(self, platform_name, monkeypatch, tmp_path):
        self.platform = platform_name
        self.root = tmp_path / "PrivacyFence"
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(self.root))
        privilege_separation.reset_cache()
        yield
        privilege_separation.reset_cache()

    def test_an_empty_owner_still_parses(self):
        _write_marker(self.root, self.platform, owner_user="")

        state = privilege_separation.separation()

        assert state is not None
        assert state.owner_user == ""
        assert state.service_account == service_account_of(self.platform)

    def test_an_empty_owner_is_still_a_separated_install(self):
        # The whole point of the split: what is pending is one group
        # membership, not the separation. Reading this as "not separated"
        # would send paths.py back to the agent-writable data directory.
        _write_marker(self.root, self.platform, owner_user="")

        assert privilege_separation.is_enabled() is True
        assert privilege_separation.data_dir_override() == self.root

    def test_an_absent_owner_is_still_refused(self):
        # The distinction the machine half depends on: "" is a value, a
        # missing key is a marker this build cannot interpret.
        payload = _marker_payload(self.platform)
        del payload["owner_user"]
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / privilege_separation.MARKER_FILE_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )
        privilege_separation.reset_cache()

        assert privilege_separation.separation() is None

    def test_the_marker_version_has_not_moved(self):
        # Asserted as a literal on purpose: every other check in this file
        # compares the scripts against MARKER_VERSION, so all of them would
        # follow a bump silently. This is the one that would not.
        assert privilege_separation.MARKER_VERSION == 1


class TestEnableSplitContract:
    """ADR 0003 decision 3, as the three provisioning scripts spell it."""

    SCRIPTS = {platform: INSTALLERS[platform].read_text(encoding="utf-8") for platform in PLATFORMS}

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_the_posix_scripts_have_a_per_user_half(self, platform):
        script = self.SCRIPTS[platform]
        assert "cmd_enable_for_user()" in script
        assert "--for-user)" in script
        assert "FOR_USER_ONLY=1" in script

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_the_posix_machine_half_never_dies_on_an_unresolved_owner(self, platform):
        # resolve_owner() die()s on one; resolve_owner_optional() is the shape
        # status already carried, and is what cmd_enable takes now. A drift
        # back to the strict one is the single change that would restore the
        # "leaves the install opt-in" fallback this decision removes.
        body = re.search(
            r"^cmd_enable\(\) \{\n(.*?)^\}", self.SCRIPTS[platform], re.MULTILINE | re.DOTALL
        )
        assert body is not None
        assert "resolve_owner_optional" in body.group(1)
        assert re.search(r"^\s+resolve_owner$", body.group(1), re.MULTILINE) is None

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_the_posix_group_add_is_the_only_conditional_step(self, platform):
        # Everything root can do alone runs unconditionally; the group
        # membership is the one step behind an owner check.
        body = re.search(
            r"^cmd_enable\(\) \{\n(.*?)^\}", self.SCRIPTS[platform], re.MULTILINE | re.DOTALL
        ).group(1)
        assert re.search(
            r'if \[ -n "\$OWNER_USER" \]; then\n\s+add_owner_to_service_group', body
        )
        for step in ("create_service_account", "migrate_data", "apply_layout", "write_marker"):
            assert re.search(rf"^  {step}$", body, re.MULTILINE), step

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_the_posix_machine_half_has_nothing_to_migrate(self, platform):
        # With no owner there is no home directory to read, so migrate_data
        # reduces to creating the root -- the path it already had for "no
        # existing ~/.privacyfence".
        body = re.search(
            r"^migrate_data\(\) \{\n(.*?)^\}", self.SCRIPTS[platform], re.MULTILINE | re.DOTALL
        ).group(1)
        assert re.search(r'if \[ -z "\$OWNER_HOME" \]; then', body)
        assert 'mkdir -p "$SYSTEM_ROOT"' in body

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    def test_the_posix_scripts_report_the_pending_state_distinctly(self, platform):
        # Otherwise there is nothing for a human -- or the companion -- to
        # read: "PENDING USER" is separated-with-a-step-left, which is not
        # the same answer as "OFF".
        script = self.SCRIPTS[platform]
        assert "PENDING USER" in script
        assert "marker_owner_user()" in script

    def test_the_windows_script_has_a_per_user_half(self):
        script = self.SCRIPTS["win32"]
        assert "function Invoke-EnableForUser {" in script
        assert "[string] $ForUser," in script
        assert "Invoke-EnableForUser" in script.split("switch ($Command) {", 1)[1]

    def test_the_windows_machine_half_never_throws_on_an_unresolved_owner(self):
        body = re.search(
            r"^function Invoke-Enable \{\n(.*?)^\}", self.SCRIPTS["win32"], re.MULTILINE | re.DOTALL
        )
        assert body is not None
        assert "Resolve-Owner -Optional" in body.group(1)
        assert re.search(r"^\s+Resolve-Owner$", body.group(1), re.MULTILINE) is None
        assert re.search(
            r"if \(\$script:OwnerResolved\) \{\n\s+Add-OwnerToServiceGroup", body.group(1)
        )

    def test_the_windows_group_creation_and_the_group_add_are_two_steps(self):
        # New-ServiceGroup used to do both, which is exactly what made the
        # machine half impossible on this platform.
        body = re.search(
            r"^function New-ServiceGroup \{\n(.*?)^\}", self.SCRIPTS["win32"], re.MULTILINE | re.DOTALL
        ).group(1)
        assert "Add-LocalGroupMember" not in body
        assert "function Add-OwnerToServiceGroup {" in self.SCRIPTS["win32"]

    def test_the_windows_marker_records_an_empty_owner_when_none_resolved(self):
        assert (
            "owner_user      = $(if ($script:OwnerResolved) { $script:OwnerUser } else { '' })"
            in self.SCRIPTS["win32"]
        )

    def test_the_windows_script_reports_the_pending_state_distinctly(self):
        assert "PENDING USER" in self.SCRIPTS["win32"]
        assert "function Get-MarkerOwnerUser {" in self.SCRIPTS["win32"]

    @pytest.mark.parametrize("platform", POSIX_PLATFORMS)
    @pytest.mark.skipif(
        sys.platform == "win32", reason="runs a bash script; Windows' installer is the .ps1"
    )
    def test_the_posix_scripts_really_parse_the_flag(self, platform):
        # The one assertion in this class that is not a read of the source:
        # an option the `case` doesn't know dies at "unknown option" before
        # any subcommand runs, and that is a parse failure no grep would
        # catch. Whichever of the two refusals this reaches -- "run this with
        # sudo" on Linux, "macOS-only" on the other -- is past the parser.
        result = subprocess.run(
            ["bash", str(INSTALLERS[platform]), "enable", "--for-user", "alice"],
            capture_output=True, text=True, timeout=30, check=False,
        )

        assert result.returncode != 0
        assert "unknown option" not in result.stderr

    def test_the_pkg_postinstall_provisions_without_a_console_user(self):
        # The .pkg's own half of decision 3: "nobody is logged in" used to
        # exit 0 before running anything, which is the fallback the ADR's
        # Context table names first.
        postinstall = (REPO_ROOT / "installer" / "macos" / "pkg" / "postinstall").read_text(
            encoding="utf-8"
        )

        assert 'CONSOLE_USER=""' in postinstall
        assert '"$SEPARATION_SCRIPT" enable --auto --app "$APP_PATH"' in postinstall


class TestDebPostinstFailurePolicy:
    """ADR 0003 decision 5: the ``.deb``'s ``postinst`` stops being
    best-effort. The two halves of decision 3 are invoked as two calls
    precisely because they have two different failure policies -- the machine
    half fails the package install, the per-user half is allowed to defer --
    and a single call could not express both."""

    SCRIPT = INSTALLERS["linux"].read_text(encoding="utf-8")
    POSTINST = (REPO_ROOT / "debian" / "postinst").read_text(encoding="utf-8")

    @staticmethod
    def _separation_block() -> str:
        block = re.search(
            r'^if \[ "\$1" = "configure" \] && \[ -x /usr/sbin/privacyfence-privilege-separation'
            r' \]; then\n(.*?)^fi$',
            TestDebPostinstFailurePolicy.POSTINST,
            re.MULTILINE | re.DOTALL,
        )
        assert block is not None, "no `configure` privilege-separation block in debian/postinst"
        return block.group(1)

    def test_the_machine_half_is_unconditional_and_loud(self):
        # The whole of decision 5, in one assertion: no `|| true`, and no `if`
        # around it. A failure of this is a failure of the install -- under
        # decision 1 an install that could not separate itself is not a
        # less-hardened PrivacyFence, it is one whose central claim does not
        # hold, which is not the ancillary thing a postinst is meant to shrug
        # off.
        machine_half = [
            line for line in self._separation_block().splitlines()
            if "enable --machine-only" in line
        ]
        assert len(machine_half) == 1, self._separation_block()
        line = machine_half[0]
        assert "|| true" not in line
        # Four spaces: the block's own indentation level, i.e. not nested
        # inside a condition of its own the way the per-user half is.
        assert line.startswith("    /usr/sbin/"), line

    def test_the_per_user_half_keeps_sudo_user_and_keeps_deferring(self):
        # The half that genuinely needs to know which human, and the only
        # thing in a postinst's environment that can say. Unresolvable is a
        # supported state (`status` reports PENDING USER, the companion closes
        # it at the first login session), so it stays conditional and stays
        # non-fatal -- `--auto` for the failures the script can see from the
        # inside, `|| true` for the ones it cannot.
        block = self._separation_block()
        per_user = re.search(
            r'if \[ -n "\$\{SUDO_USER:-\}" \] && \[ "\$SUDO_USER" != "root" \]; then\n(.*?)\n    fi',
            block, re.DOTALL,
        )
        assert per_user is not None, block
        assert "enable --auto --for-user \"$SUDO_USER\"" in per_user.group(1)
        assert "|| true" in per_user.group(1)

    def test_the_linux_script_has_a_machine_only_flag(self):
        # Without it the postinst's first call would pick $SUDO_USER out of
        # its own environment and do the group add on the call that is not
        # allowed to fail -- putting the one step decision 3 lets defer inside
        # the one call decision 5 makes fatal.
        assert "--machine-only)" in self.SCRIPT
        assert "MACHINE_ONLY=1" in self.SCRIPT
        body = re.search(
            r"^cmd_enable\(\) \{\n(.*?)^\}", self.SCRIPT, re.MULTILINE | re.DOTALL
        ).group(1)
        assert re.search(
            r'if \[ "\$MACHINE_ONLY" = "1" \]; then\n.*?\n  else\n\s+resolve_owner_optional', body,
            re.DOTALL,
        ), body

    def test_the_machine_half_never_unrecords_an_owner(self):
        # It re-runs on every install and upgrade now, against installs whose
        # per-user half is already closed. Writing "" over the recorded name
        # would report a complete install as PENDING USER and have the
        # companion re-run the per-user half at every login to fix nothing.
        body = re.search(
            r"^write_marker\(\) \{\n(.*?)^\}", self.SCRIPT, re.MULTILINE | re.DOTALL
        ).group(1)
        assert re.search(
            r'if \[ -z "\$recorded_owner" \] && \[ -f "\$marker" \]; then\n'
            r'\s+recorded_owner="\$\(marker_owner_user\)"',
            body,
        ), body
        assert '"owner_user": "${recorded_owner}"' in body

    def test_the_per_user_half_bounces_the_daemon_around_a_real_migration(self):
        # It runs against a *live* separated install by construction now: the
        # postinst calls it moments after the machine half started the unit.
        # Merging a legacy ~/.privacyfence into a directory the running daemon
        # has open would corrupt whichever copy lost.
        body = re.search(
            r"^cmd_enable_for_user\(\) \{\n(.*?)^\}", self.SCRIPT, re.MULTILINE | re.DOTALL
        ).group(1)
        assert 'if [ -d "$(legacy_data_dir)" ] && daemon_unit_is_active; then' in body
        assert 'systemctl stop "$DAEMON_UNIT"' in body
        assert 'systemctl start "$DAEMON_UNIT"' in body
        assert body.index("systemctl stop") < body.index("  migrate_data") < body.index(
            "systemctl start"
        )

    @pytest.mark.skipif(
        sys.platform == "win32", reason="runs a bash script; Windows' installer is the .ps1"
    )
    def test_machine_only_and_an_owner_are_refused_together(self):
        # Two complements, not a modifier and a modified: a caller that names
        # a user and then asks for the half that deliberately has none means
        # one of the two and typed both. Picking silently is the difference
        # between an install whose owner is in the group and one whose isn't.
        for flag in (["--user", "alice"], ["--for-user", "alice"]):
            result = subprocess.run(
                ["bash", str(INSTALLERS["linux"]), "enable", "--machine-only", *flag],
                capture_output=True, text=True, timeout=30, check=False,
            )

            assert result.returncode != 0
            assert "cannot be combined" in result.stderr, result.stderr


_NET_LOCALGROUP_OUTPUT = """\
Alias name     PrivacyFenceUsers
Comment        May read PrivacyFence's handoff directory.

Members

-------------------------------------------------------------------------------
DESKTOP-7Q1\\alice
bob
The command completed successfully.

"""


class TestServiceGroupMembers:
    """ADR 0003 decision 3's read side. Deliberately the *recorded*
    membership rather than this process's own token -- see the function's
    own docstring for why reading the token would prompt at every start
    until the human logged out."""

    @staticmethod
    def _fake_grp(monkeypatch, getgrnam):
        # Injected into sys.modules rather than monkeypatched onto the real
        # module: ``grp`` is POSIX-only and this file is collected on Windows
        # too, where importing it at all is a ModuleNotFoundError. The code
        # under test imports it inside the function, after its own platform
        # check, so the injected module is what it picks up -- which keeps
        # the POSIX branch covered on every runner rather than skipped on one.
        monkeypatch.setitem(sys.modules, "grp", SimpleNamespace(getgrnam=getgrnam))

    def test_posix_reads_the_group_file(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        self._fake_grp(monkeypatch, lambda name: SimpleNamespace(gr_mem=["alice", "bob"]))

        assert privilege_separation.service_group_members("privacyfence") == {"alice", "bob"}

    def test_posix_answers_none_for_a_group_that_does_not_exist(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")

        def _missing(name):
            raise KeyError(name)

        self._fake_grp(monkeypatch, _missing)

        assert privilege_separation.service_group_members("privacyfence") is None

    def test_windows_reads_net_localgroup(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        calls = []

        def _run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, _NET_LOCALGROUP_OUTPUT, "")

        monkeypatch.setattr(privilege_separation.subprocess, "run", _run)

        members = privilege_separation.service_group_members("PrivacyFenceUsers")

        assert calls[0][1:] == ["localgroup", "PrivacyFenceUsers"]
        # Resolved against %SystemRoot%, not left as a bare "net": bandit
        # B607's own reason, and the same one _OSASCRIPT is absolute for.
        assert calls[0][0].endswith("System32/net.exe") or calls[0][0].endswith("System32\\net.exe")
        assert {"alice", "bob"} <= members

    def test_windows_answers_none_when_net_fails(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setattr(
            privilege_separation.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, "", "not found"),
        )

        assert privilege_separation.service_group_members("PrivacyFenceUsers") is None

    def test_windows_answers_none_when_net_cannot_run(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")

        def _boom(argv, **kwargs):
            raise OSError("no net.exe")

        monkeypatch.setattr(privilege_separation.subprocess, "run", _boom)

        assert privilege_separation.service_group_members("PrivacyFenceUsers") is None

    def test_the_domain_qualifier_is_stripped(self):
        # current_user_name() answers with the account half on Windows
        # (windows_acl.current_account_name()), so a DOMAIN\ prefix left on
        # would never match and would prompt forever.
        assert "alice" in privilege_separation._parse_net_localgroup(_NET_LOCALGROUP_OUTPUT)

    def test_nothing_before_the_separator_is_a_member(self):
        parsed = privilege_separation._parse_net_localgroup(_NET_LOCALGROUP_OUTPUT)

        assert "Alias name     PrivacyFenceUsers" not in parsed
        assert "Members" not in parsed


class TestOwnerMembershipPending:
    def test_an_unseparated_install_is_never_pending(self, platform_name, monkeypatch, tmp_path):
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        privilege_separation.reset_cache()

        assert privilege_separation.owner_membership_pending() is False

    def test_a_user_in_the_group_is_not_pending(self, separated, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "alice")
        monkeypatch.setattr(
            privilege_separation, "service_group_members", lambda group: frozenset({"alice"})
        )

        assert privilege_separation.owner_membership_pending() is False

    def test_the_owner_outside_the_group_is_pending(self, separated, monkeypatch):
        # The marker's own owner_user is "alice" (_marker_payload's default)
        # -- pending is still the right answer for the account the install
        # was actually provisioned for.
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "alice")
        monkeypatch.setattr(privilege_separation, "service_group_members", lambda group: frozenset())

        assert privilege_separation.owner_membership_pending() is True

    def test_a_different_account_outside_the_group_is_pending(self, separated, monkeypatch):
        # ADR 0008 retired the local-mode-fixes plan's Phase 2 §2.6 interim
        # guard: the marker names "alice" as owner, but "bob" is a second
        # account this install's separated daemon can now give an isolated
        # principal of their own -- so "bob" outside the group is pending
        # exactly like the owner always was, not refused.
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "bob")
        monkeypatch.setattr(
            privilege_separation, "service_group_members", lambda group: frozenset({"alice"})
        )

        assert privilege_separation.owner_membership_pending() is True

    def test_an_unreadable_group_falls_back_to_an_empty_marker_owner(
        self, platform_name, monkeypatch, tmp_path
    ):
        root = tmp_path / "PrivacyFence"
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
        _write_marker(root, platform_name, owner_user="")
        monkeypatch.setattr(privilege_separation, "service_group_members", lambda group: None)

        assert privilege_separation.owner_membership_pending() is True

    def test_an_unreadable_group_with_a_named_owner_is_not_pending(
        self, platform_name, monkeypatch, tmp_path
    ):
        # The safe direction: guessing "pending" on a platform we just failed
        # to interrogate would put a password dialog in front of somebody at
        # every single companion start. current_user_name is pinned to the
        # marker's own owner so this exercises that fallback specifically,
        # not the (also-False, but unrelated) owner-mismatch guard.
        root = tmp_path / "PrivacyFence"
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
        _write_marker(root, platform_name, owner_user="alice")
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "alice")
        monkeypatch.setattr(privilege_separation, "service_group_members", lambda group: None)

        assert privilege_separation.owner_membership_pending() is False


class TestOwnerUidAndSid:
    """ADR 0008's own identity mapping: ``owner_uid()``/``owner_sid()`` are
    what ``web/control_channel.py``'s ``principal_id_for_peer()`` compares a
    connecting peer against to decide whether it is this install's owner
    (``LOCAL_PRINCIPAL``) or a second, isolated ``os-<uid>``/``os-<sid>``
    principal -- the direct replacement for the retired
    ``other_account_owns_this_install()`` refusal."""

    pytestmark = posix_permissions_only

    def test_unseparated_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        privilege_separation.reset_cache()

        assert privilege_separation.owner_uid() is None
        assert privilege_separation.owner_sid() is None

    def test_no_recorded_owner_is_none(self, platform_name, monkeypatch, tmp_path):
        if platform_name == "win32":
            pytest.skip("POSIX only")
        root = tmp_path / "PrivacyFence"
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
        _write_marker(root, platform_name, owner_user="")

        assert privilege_separation.owner_uid() is None

    def test_resolves_a_real_account_on_posix(self, separated, platform_name):
        if platform_name == "win32":
            pytest.skip("POSIX only")
        import pwd

        _write_marker(separated, platform_name, owner_user=this_account())

        assert privilege_separation.owner_uid() == pwd.getpwnam(this_account()).pw_uid

    def test_none_for_an_account_that_does_not_exist(self, separated, platform_name):
        if platform_name == "win32":
            pytest.skip("POSIX only")
        _write_marker(separated, platform_name, owner_user="no-such-account-anywhere")

        assert privilege_separation.owner_uid() is None

    def test_owner_sid_is_none_on_posix(self, separated, platform_name):
        if platform_name == "win32":
            pytest.skip("Windows only")
        assert privilege_separation.owner_sid() is None

    def test_owner_uid_is_none_on_windows(self, separated, platform_name):
        if platform_name != "win32":
            pytest.skip("Windows only")
        assert privilege_separation.owner_uid() is None

    def test_owner_sid_delegates_to_the_real_resolver_on_windows(self, separated, platform_name, monkeypatch):
        # _resolve_owner_sid() itself is a real win32 API call (pragma:
        # no cover -- exercised by the platform-windows job); what's
        # testable here on any platform is that owner_sid() only calls it
        # once its own guards (separated, owner recorded, actually
        # windows) all pass, with the marker's own owner_user.
        if platform_name != "win32":
            pytest.skip("Windows only")
        monkeypatch.setattr(
            privilege_separation, "_resolve_owner_sid",
            lambda owner_user: f"S-1-5-21-fake-for-{owner_user}",
        )

        assert privilege_separation.owner_sid() == "S-1-5-21-fake-for-alice"


class TestInstallerScriptResolution:
    def test_macos_defers_to_the_bundled_resolver(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        script = tmp_path / "macos_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(privilege_separation, "_macos_installer_script_path", lambda: script)

        assert privilege_separation.installer_script_path() == script

    def test_linux_prefers_what_the_deb_installs(self, monkeypatch, tmp_path):
        # Relocated rather than asserted at its real path: /usr/sbin is not a
        # place a test may write, and on the Windows runner this file is also
        # collected on it is not even a path that can exist.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        packaged = tmp_path / "privacyfence-privilege-separation"
        packaged.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(privilege_separation, "LINUX_PACKAGED_INSTALLER", packaged)

        assert privilege_separation.installer_script_path() == packaged

    def test_the_packaged_linux_path_is_the_one_the_deb_writes(self):
        # The constant above is only a test seam if it still names what
        # build_deb.sh actually installs -- and what the status command in
        # PLATFORM_LAYOUTS quotes at a human reading a daemon log.
        build_deb = (REPO_ROOT / "scripts" / "build_deb.sh").read_text(encoding="utf-8")

        assert privilege_separation.LINUX_PACKAGED_INSTALLER.as_posix() == (
            "/usr/sbin/privacyfence-privilege-separation"
        )
        assert f'{privilege_separation.LINUX_PACKAGED_INSTALLER.as_posix()}"' in build_deb

    def test_linux_falls_back_to_the_checkout(self, monkeypatch, tmp_path):
        # The packaged path is pointed at nothing explicitly rather than left
        # to be absent: on a developer's own Debian box the .deb really is
        # installed, and the fallback is what this test is about.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(
            privilege_separation, "LINUX_PACKAGED_INSTALLER", tmp_path / "not-installed"
        )

        assert privilege_separation.installer_script_path() == INSTALLERS["linux"]

    def test_windows_looks_next_to_the_running_executable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        app = tmp_path / "privacyfence-app.exe"
        app.write_text("", encoding="utf-8")
        (tmp_path / "privilege-separation.ps1").write_text("", encoding="utf-8")
        monkeypatch.setattr(privilege_separation.sys, "executable", str(app))

        assert privilege_separation.installer_script_path() == tmp_path / "privilege-separation.ps1"

    def test_windows_falls_back_to_the_checkout(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setattr(privilege_separation.sys, "executable", str(tmp_path / "python"))

        assert privilege_separation.installer_script_path() == INSTALLERS["win32"]

    def test_none_when_nothing_is_on_disk(self, monkeypatch, tmp_path):
        # A pip install on a machine with no .deb and no checkout next to the
        # package -- which is the case decision 6's gate later has to be able
        # to report rather than crash on.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(
            privilege_separation, "_checkout_script_path", lambda name: tmp_path / name
        )
        monkeypatch.setattr(
            privilege_separation, "LINUX_PACKAGED_INSTALLER", tmp_path / "not-installed"
        )

        assert privilege_separation.installer_script_path() is None

    def test_the_first_candidate_that_exists_wins(self, tmp_path):
        present = tmp_path / "present"
        present.write_text("", encoding="utf-8")

        assert privilege_separation._first_existing(tmp_path / "absent", present) == present
        assert privilege_separation._first_existing(tmp_path / "absent") is None


class TestElevationScriptProblem:
    """#428 B2, now guarding a second elevation. The rule it encodes is the
    same one: the only thing this module ever runs as root is a script an
    installer shipped, never one the account being separated from can
    rewrite."""

    def test_posix_refuses_a_script_this_account_owns(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        script = tmp_path / "linux_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        # Faked rather than read off disk: the uid this test needs is "not
        # root", and CI's own runner is sometimes exactly root.
        monkeypatch.setattr(
            privilege_separation.os, "stat",
            lambda path: os.stat_result((0o100755, 0, 0, 1, 501, 0, 0, 0, 0, 0)),
        )

        problem = privilege_separation._elevation_script_problem(script)

        assert problem is not None
        assert "not root" in problem

    def test_posix_refuses_a_script_that_is_not_there(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")

        problem = privilege_separation._elevation_script_problem(tmp_path / "gone.sh")

        assert problem is not None
        assert "could not stat" in problem

    def test_posix_accepts_a_root_owned_unwritable_script(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        script = tmp_path / "linux_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(
            privilege_separation.os, "stat",
            lambda path: os.stat_result((0o100755, 0, 0, 1, 0, 0, 0, 0, 0, 0)),
        )

        assert privilege_separation._elevation_script_problem(script) is None

    def test_posix_refuses_a_group_writable_script(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        script = tmp_path / "linux_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(
            privilege_separation.os, "stat",
            lambda path: os.stat_result((0o100775, 0, 0, 1, 0, 0, 0, 0, 0, 0)),
        )

        problem = privilege_separation._elevation_script_problem(script)

        assert problem is not None
        assert "group- or world-writable" in problem

    def test_macos_still_checks_the_bundle_signature(self, monkeypatch, tmp_path):
        # The refactor that gave Linux the POSIX half must not have cost
        # macOS the codesign half.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        called = []
        monkeypatch.setattr(
            privilege_separation, "_macos_auto_enable_script_problem",
            lambda script: called.append(script) or "nope",
        )

        assert privilege_separation._elevation_script_problem(tmp_path / "x") == "nope"
        assert called == [tmp_path / "x"]

    def test_windows_refuses_an_acl_it_cannot_read(self, monkeypatch, tmp_path):
        # read_dacl() answers None off Windows and on a path with no
        # descriptor to read, and "could not check" has to read as "do not
        # elevate" in front of a UAC prompt.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")

        problem = privilege_separation._elevation_script_problem(tmp_path / "x.ps1")

        assert problem is not None
        assert "could not read" in problem

    def test_windows_refuses_a_user_writable_script(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setattr(
            windows_acl, "read_dacl",
            lambda path: [windows_acl.Ace(trustee="DESKTOP-7Q1\\alice", mask=0x1F01FF)],
        )

        problem = privilege_separation._elevation_script_problem(tmp_path / "x.ps1")

        assert problem is not None
        assert "alice" in problem

    def test_windows_accepts_an_administrators_only_script(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setattr(
            windows_acl, "read_dacl",
            lambda path: [
                windows_acl.Ace(trustee="BUILTIN\\Administrators", mask=0x1F01FF),
                windows_acl.Ace(trustee="BUILTIN\\Users", mask=0x1200A9),
            ],
        )

        assert privilege_separation._elevation_script_problem(tmp_path / "x.ps1") is None


def _command_line_to_argv(command_line: str) -> list[str]:
    """``CommandLineToArgvW``, in Python, for the arguments half of a Windows
    command line (no executable name).

    ``subprocess.list2cmdline`` builds the elevated PowerShell's command line
    by exactly these rules, and ``Start-Process -ArgumentList`` hands it over
    verbatim, so this is what that process really receives -- which is the
    only question the Windows tests below are asking. Substring-matching the
    launcher string cannot answer it: every argument is quoted twice on the
    way there (once for the inner ``&`` call, once for the outer
    ``-ArgumentList`` literal), so an argument that had torn in half would
    still match.
    """
    args: list[str] = []
    current: list[str] = []
    in_quotes = False
    started = False
    index = 0
    while index < len(command_line):
        char = command_line[index]
        if char == "\\":
            backslashes = 0
            while index < len(command_line) and command_line[index] == "\\":
                backslashes += 1
                index += 1
            if index < len(command_line) and command_line[index] == '"':
                current.append("\\" * (backslashes // 2))
                if backslashes % 2:
                    current.append('"')
                else:
                    in_quotes = not in_quotes
                index += 1
            else:
                current.append("\\" * backslashes)
            started = True
        elif char == '"':
            if in_quotes and command_line[index + 1:index + 2] == '"':
                current.append('"')
                index += 2
            else:
                in_quotes = not in_quotes
                index += 1
            started = True
        elif char in " \t" and not in_quotes:
            if started:
                args.append("".join(current))
                current = []
                started = False
            index += 1
        else:
            current.append(char)
            started = True
            index += 1
    if started:
        args.append("".join(current))
    return args


def _elevated_argv(launcher_command: str) -> list[str]:
    """The argv the UAC-elevated PowerShell receives, peeled back out of the
    launcher's ``-Command``.

    Two layers, and both are real: ``-ArgumentList`` carries the whole inner
    command line as one single-quoted PowerShell literal (where doubling the
    quote is the entire escaping rule), and that command line is itself
    quoted by Windows' own rules. This undoes them in that order.
    """
    marker = "-ArgumentList '"
    start = launcher_command.index(marker) + len(marker)
    index = start
    while True:
        index = launcher_command.index("'", index)
        if launcher_command[index + 1:index + 2] == "'":
            index += 2
            continue
        break
    return _command_line_to_argv(launcher_command[start:index].replace("''", "'"))


class TestPerUserElevationCommand:
    @pytest.fixture
    def transcript(self, tmp_path):
        """Where the elevated child's output is collected. Only the Windows
        branch has any use for it -- see ``_elevation_transcript()`` for why
        only that platform needs a file to get output back at all."""
        return tmp_path / "elevated.log"

    def test_macos_reuses_the_admin_prompt(self, monkeypatch, tmp_path, transcript):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        script = tmp_path / "macos_privilege_separation.sh"

        argv = privilege_separation._per_user_argv(script, "alice", transcript)

        assert argv[0] == privilege_separation._OSASCRIPT
        assert "with administrator privileges" in argv[-1]
        assert "enable --for-user alice" in argv[-1]
        # Not --auto, which maybe_auto_enable_macos() does pass: that mode
        # exits 0 on every failure path, which would report a separation
        # that did not happen as one the human should now log out for.
        assert "--auto" not in argv[-1]

    def test_macos_quotes_a_hostile_account_name(self, monkeypatch, tmp_path, transcript):
        # Two layers, neither substituting for the other: shlex for the shell
        # `do shell script` runs, then AppleScript's own string escaping on
        # top of it. Asserted by peeling both back off and checking the argv
        # the shell would actually see -- one argument, not three.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        script = tmp_path / "s.sh"
        hostile = 'a"; rm -rf /; #'

        argv = privilege_separation._per_user_argv(script, hostile, transcript)

        literal = argv[-1][len('do shell script "'):-len('" with administrator privileges')]
        command = re.sub(r"\\(.)", r"\1", literal)
        assert shlex.split(command) == [str(script), "enable", "--for-user", hostile]

    def test_windows_elevates_through_uac(self, monkeypatch, tmp_path, transcript):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")

        argv = privilege_separation._per_user_argv(
            tmp_path / "privilege-separation.ps1", "alice", transcript
        )

        assert argv[0].endswith("powershell.exe")
        command = argv[-1]
        assert "-Verb RunAs" in command
        # -Wait *and* -PassThru, or the return code below would be the
        # launcher's rather than the script's and every failure would read as
        # a success -- privacyfence/privacyfence#599, where it did, on every
        # run, with the daemon logging a separation that had not happened.
        assert "-Wait" in command
        assert "-PassThru" in command
        assert "exit $p.ExitCode" in command

        elevated = _elevated_argv(command)
        assert elevated[:4] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
        # -ForUser bare, alice quoted, and that asymmetry is the point:
        # PowerShell binds a parameter *name* only while it is a bare token,
        # so quoting it uniformly would send three positional arguments that
        # bind nothing, while leaving the account name bare would let it be
        # read as one.
        assert "enable -ForUser 'alice'" in elevated[-1]
        assert str(transcript) in elevated[-1]

    def test_windows_doubles_a_quote_in_an_account_name(self, monkeypatch, tmp_path, transcript):
        # Doubling the quote is the whole of PowerShell's escaping rule, and
        # it applies once per layer: once to the account name inside the `&`
        # call, then again to the whole inner command line on its way through
        # the outer -ArgumentList. Asserted by peeling both back off rather
        # than by counting quotes in the launcher string.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")

        argv = privilege_separation._per_user_argv(tmp_path / "s.ps1", "al'ice", transcript)

        elevated_command = _elevated_argv(argv[-1])[-1]
        assert "-ForUser 'al''ice'" in elevated_command
        # And nowhere does it appear unescaped, which is what would end the
        # string literal early and leave the rest of the name as code.
        assert "al'ice" not in elevated_command.replace("al''ice", "")

    def test_windows_quotes_an_install_path_with_a_space_in_it(self, monkeypatch, transcript):
        # The path every real install has. Start-Process joins an
        # -ArgumentList *array* with plain spaces and quotes nothing, so the
        # array this used to pass reached the elevated PowerShell as
        # `-File C:\Program Files\...` -- which reads `C:\Program` as the
        # script, cannot find it, and exits nonzero after the user has
        # already approved the UAC prompt. With the daemon task repeating
        # every five minutes, that is a prompt that comes back forever and
        # can never accomplish anything.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        script = Path(r"C:\Program Files\PrivacyFence\privilege-separation.ps1")

        for argv in (
            privilege_separation._per_user_argv(script, "alice", transcript),
            privilege_separation._windows_full_enable_argv(script, transcript),
        ):
            assert argv is not None
            # `& '<path>'` is -File spelled as an expression; the redirection
            # #599 needs has to be established inside the elevated process,
            # which -File has no room for. The guarantee is unchanged: the
            # path reaches the elevated PowerShell as one argument, whatever
            # it contains.
            elevated = _elevated_argv(argv[-1])
            assert len(elevated) == 5, elevated
            assert elevated[-1].startswith(f"try {{ & '{script}' "), elevated[-1]

    def test_powershell_quoting_doubles_only_the_quote(self):
        # Backslashes are literal in a single-quoted PowerShell string, so
        # escaping them the way shlex would corrupts every path here.
        assert privilege_separation._powershell_quoted("C:\\x\\y") == "'C:\\x\\y'"

    def test_linux_uses_pkexec(self, monkeypatch, tmp_path, transcript):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: "/usr/bin/pkexec")

        argv = privilege_separation._per_user_argv(tmp_path / "s.sh", "alice", transcript)

        assert argv == ["/usr/bin/pkexec", str(tmp_path / "s.sh"), "enable", "--for-user", "alice"]

    def test_linux_declines_to_guess_without_pkexec(self, monkeypatch, tmp_path, transcript):
        # A bare `sudo` from an XDG-autostarted process hangs on a password
        # prompt nobody can see, which is worse than saying so.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: None)

        assert privilege_separation._per_user_argv(tmp_path / "s.sh", "alice", transcript) is None

    @pytest.mark.parametrize("platform,expected", [("linux", "sudo"), ("darwin", "sudo"), ("win32", "powershell")])
    def test_the_typed_command_matches_the_platform(self, monkeypatch, tmp_path, platform, expected):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: platform)

        text = privilege_separation.per_user_command_text(tmp_path / "s", "alice")

        assert text.startswith(expected)
        assert "alice" in text


class TestCompletePerUserSeparation:
    @pytest.fixture
    def script(self, tmp_path, monkeypatch):
        path = tmp_path / "privilege-separation"
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: path)
        monkeypatch.setattr(privilege_separation, "_elevation_script_problem", lambda s: None)
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "alice")
        return path

    @pytest.fixture(autouse=True)
    def group_took_the_addition(self, monkeypatch):
        """The ordinary outcome of a successful elevated run: the account is
        in the service group afterwards.

        Default rather than per-test because every test below that expects
        True now depends on it -- privacyfence/privacyfence#599 made an exit
        code alone insufficient to return True, and this is the machine state
        that makes it sufficient. The tests that care about the *absence* of
        this override it back."""
        monkeypatch.setattr(
            privilege_separation, "service_group_members", lambda group: frozenset({"alice", "bob"})
        )

    def test_does_nothing_on_an_unseparated_install(self, platform_name, monkeypatch, tmp_path):
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        privilege_separation.reset_cache()

        assert privilege_separation.complete_per_user_separation() is False

    def test_reports_a_missing_provisioning_script(self, separated, monkeypatch, caplog):
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: None)
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "alice")

        with caplog.at_level("WARNING"):
            assert privilege_separation.complete_per_user_separation() is False
        assert "alice" in caplog.text

    def test_refuses_a_script_that_fails_its_safety_check(self, separated, script, monkeypatch, caplog):
        monkeypatch.setattr(
            privilege_separation, "_elevation_script_problem", lambda s: "owned by uid 501"
        )

        with caplog.at_level("WARNING"):
            assert privilege_separation.complete_per_user_separation() is False
        assert "owned by uid 501" in caplog.text
        # And still names what to type, rather than leaving a dead end.
        assert "enable" in caplog.text

    def test_names_the_command_when_it_cannot_ask_for_a_password(
        self, separated, script, monkeypatch, caplog
    ):
        monkeypatch.setattr(privilege_separation, "_per_user_argv", lambda s, u, t: None)

        with caplog.at_level("WARNING"):
            assert privilege_separation.complete_per_user_separation() is False
        assert "enable" in caplog.text

    def test_runs_the_elevated_command_for_this_account(self, separated, script, monkeypatch):
        calls = []

        def _run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(privilege_separation, "_per_user_argv", lambda s, u, t: ["x", u])
        monkeypatch.setattr(privilege_separation.subprocess, "run", _run)

        assert privilege_separation.complete_per_user_separation() is True
        assert calls[0][0] == ["x", "alice"]
        # Bounded, like every other elevation here: a dialog nobody answers
        # must not pin a thread forever.
        assert calls[0][1]["timeout"] == 300

    def test_an_explicit_user_wins_over_this_process(self, separated, script, monkeypatch):
        seen = []
        monkeypatch.setattr(privilege_separation, "_per_user_argv", lambda s, u, t: seen.append(u) or ["x"])
        monkeypatch.setattr(
            privilege_separation.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        )

        assert privilege_separation.complete_per_user_separation("bob") is True
        assert seen == ["bob"]

    def test_a_declined_prompt_is_not_a_crash(self, separated, script, monkeypatch, caplog):
        monkeypatch.setattr(privilege_separation, "_per_user_argv", lambda s, u, t: ["x"])
        monkeypatch.setattr(
            privilege_separation.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "User cancelled."),
        )

        with caplog.at_level("INFO"):
            assert privilege_separation.complete_per_user_separation() is False
        assert "User cancelled." in caplog.text

    def test_a_subprocess_failure_is_swallowed(self, separated, script, monkeypatch):
        def _boom(argv, **kwargs):
            raise OSError("no such binary")

        monkeypatch.setattr(privilege_separation, "_per_user_argv", lambda s, u, t: ["x"])
        monkeypatch.setattr(privilege_separation.subprocess, "run", _boom)

        assert privilege_separation.complete_per_user_separation() is False

    def test_success_drops_the_cached_marker(self, separated, script, monkeypatch):
        # The marker on disk has just been rewritten with this owner in it,
        # so a cached parse from before would still read as pending.
        privilege_separation.separation()
        reset = []
        monkeypatch.setattr(privilege_separation, "reset_cache", lambda: reset.append(True))
        monkeypatch.setattr(privilege_separation, "_per_user_argv", lambda s, u, t: ["x"])
        monkeypatch.setattr(
            privilege_separation.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        )

        assert privilege_separation.complete_per_user_separation() is True
        assert reset == [True]

    def test_an_exit_zero_that_changed_nothing_is_not_success(
        self, separated, script, monkeypatch, caplog
    ):
        # privacyfence/privacyfence#599, in the half that reaches a person:
        # returning True here makes companion.py tell somebody to sign out
        # and back in, and a sign-out that fixes nothing is worse advice than
        # none. On Windows the exit code being trusted here was not even the
        # script's -- `Start-Process -Verb RunAs -Wait` without -PassThru
        # exits 0 whatever the elevated run did.
        monkeypatch.setattr(
            privilege_separation, "service_group_members", lambda group: frozenset({"bob"})
        )
        monkeypatch.setattr(privilege_separation, "_per_user_argv", lambda s, u, t: ["x"])
        monkeypatch.setattr(
            privilege_separation.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        )

        with caplog.at_level("ERROR"):
            assert privilege_separation.complete_per_user_separation() is False
        assert "still not in the" in caplog.text
        # And names the command that does it by hand, like every other
        # failure path here.
        assert "enable" in caplog.text

    def test_an_unreadable_group_is_taken_on_trust(self, separated, script, monkeypatch):
        # The same fallback owner_membership_pending() takes, and for the same
        # reason: guessing "it did not take" on a platform this process just
        # failed to interrogate would have the companion re-prompt for a
        # password at every single start.
        monkeypatch.setattr(privilege_separation, "service_group_members", lambda group: None)
        monkeypatch.setattr(privilege_separation, "_per_user_argv", lambda s, u, t: ["x"])
        monkeypatch.setattr(
            privilege_separation.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        )

        assert privilege_separation.complete_per_user_separation() is True


class TestEnableCommand:
    """ADR 0003 decision 6, Q3 from the rollout plan: PlatformLayout gains
    an enable_command so enforce_separation()'s refusal names a per-platform
    fix rather than hand-spelling one at the call site."""

    def test_every_platform_has_one(self, platform_name):
        layout = privilege_separation.PLATFORM_LAYOUTS[platform_name]
        assert layout.enable_command
        assert "enable" in layout.enable_command

    def test_distinct_from_status_and_start(self, platform_name):
        layout = privilege_separation.PLATFORM_LAYOUTS[platform_name]
        assert layout.enable_command != layout.status_command
        assert layout.enable_command != layout.start_command


class TestDevAllowsUnseparated:
    """ADR 0003 decision 7's escape hatch -- "a sibling of
    PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP", same truthy/falsy spelling."""

    def test_unset_is_false(self, monkeypatch):
        monkeypatch.delenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, raising=False)
        assert privilege_separation.dev_allows_unseparated() is False

    @pytest.mark.parametrize("value", ["", "0", "false", "False"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, value)
        assert privilege_separation.dev_allows_unseparated() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, value)
        assert privilege_separation.dev_allows_unseparated() is True


class TestEnforceSeparation:
    """ADR 0003 decision 6's own gate. Scoped to paths.is_bundled() (a
    packaged build) -- see enforce_separation()'s own docstring for why that
    is, in practice, "local mode and nothing else": org mode is never
    shipped as a frozen build, only via the wheel/sdist."""

    def test_noop_when_not_bundled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        privilege_separation.reset_cache()
        attempts = []
        monkeypatch.setattr(privilege_separation, "maybe_auto_enable_macos", lambda: attempts.append(1))
        monkeypatch.setattr(
            privilege_separation, "_run_full_auto_enable_non_macos", lambda: attempts.append(1)
        )

        privilege_separation.enforce_separation()

        assert attempts == []

    def test_noop_when_already_separated(self, separated, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        attempts = []
        monkeypatch.setattr(privilege_separation, "maybe_auto_enable_macos", lambda: attempts.append(1))
        monkeypatch.setattr(
            privilege_separation, "_run_full_auto_enable_non_macos", lambda: attempts.append(1)
        )

        privilege_separation.enforce_separation()

        assert attempts == []

    def test_darwin_dispatches_to_maybe_auto_enable_macos(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        privilege_separation.reset_cache()
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        attempts = []
        monkeypatch.setattr(privilege_separation, "maybe_auto_enable_macos", lambda: attempts.append("macos"))
        monkeypatch.setattr(
            privilege_separation, "_run_full_auto_enable_non_macos", lambda: attempts.append("other")
        )

        with pytest.raises(privilege_separation.PrivilegeSeparationError):
            privilege_separation.enforce_separation()

        assert attempts == ["macos"]

    @pytest.mark.parametrize("platform_value", ["linux", "win32"])
    def test_non_darwin_dispatches_to_the_shared_helper(self, monkeypatch, tmp_path, platform_value):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: platform_value)
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        privilege_separation.reset_cache()
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        attempts = []
        monkeypatch.setattr(privilege_separation, "maybe_auto_enable_macos", lambda: attempts.append("macos"))
        monkeypatch.setattr(
            privilege_separation, "_run_full_auto_enable_non_macos", lambda: attempts.append("other")
        )

        with pytest.raises(privilege_separation.PrivilegeSeparationError):
            privilege_separation.enforce_separation()

        assert attempts == ["other"]

    def test_hands_over_when_the_attempt_takes(self, monkeypatch, tmp_path):
        # The attempt's own side effect is provisioning the install for
        # real; simulate that by writing the marker from inside the faked
        # attempt, exactly like a real enable --auto would.
        #
        # This used to return quietly, and that was the defect: the daemon
        # that repaired its own install carried on serving the layout it had
        # just separated, as the human, past a check_runtime_identity() that
        # had already run and passed while the install was still
        # unseparated. See SeparationHandover.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        root = tmp_path / "PrivacyFence"
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
        privilege_separation.reset_cache()
        monkeypatch.setattr(paths, "is_bundled", lambda: True)

        def _fake_attempt() -> None:
            _write_marker(root, "linux")

        monkeypatch.setattr(privilege_separation, "_run_full_auto_enable_non_macos", _fake_attempt)

        with pytest.raises(privilege_separation.SeparationHandover) as exc:
            privilege_separation.enforce_separation()

        assert privilege_separation.is_enabled() is True
        message = str(exc.value)
        layout = privilege_separation.PLATFORM_LAYOUTS["linux"]
        assert layout.service_account in message
        assert layout.status_command in message
        # Decision 6's *refusal* wording must not be in it -- this outcome is
        # the one decision 6 is trying to reach, and a user who reads
        # "refusing to start" here will go looking for a problem that is not
        # there.
        assert "Refusing to start" not in message

    def test_handover_is_a_privilege_separation_error(self):
        # Every caller already stops on PrivilegeSeparationError, and
        # stopping is what this asks for -- daemon_main.main() is what tells
        # the two apart, by catching this subclass first.
        assert issubclass(
            privilege_separation.SeparationHandover, privilege_separation.PrivilegeSeparationError
        )

    def test_refuses_and_names_the_enable_command_when_the_attempt_does_not_take(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        privilege_separation.reset_cache()
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(privilege_separation, "_run_full_auto_enable_non_macos", lambda: None)

        with pytest.raises(privilege_separation.PrivilegeSeparationError) as exc:
            privilege_separation.enforce_separation()

        message = str(exc.value)
        assert "not privilege-separated" in message
        assert privilege_separation.PLATFORM_LAYOUTS["linux"].enable_command in message

    def test_no_developer_override_for_a_packaged_build(self, monkeypatch, tmp_path):
        # Decision 6's refusal is unconditional on a packaged build -- see
        # dev_allows_unseparated()'s own docstring for why the escape hatch
        # only applies to the non-packaged step_up_config.py check.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        privilege_separation.reset_cache()
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(privilege_separation, "_run_full_auto_enable_non_macos", lambda: None)

        with pytest.raises(privilege_separation.PrivilegeSeparationError):
            privilege_separation.enforce_separation()


class TestDataDirLogFilesReleased:
    """privacyfence/privacyfence#599's third defect: the elevated ``enable``
    moves the data directory out from under the process that asked for it.

    ``enforce_separation()`` runs from inside a packaged daemon that has
    already called ``daemon_main.setup_logging()``, so
    ``<data_dir>/logs/privacyfence.log`` is open for append in that very
    process for the whole elevated run -- and Windows answers a move of a
    directory holding an open file with a sharing violation rather than a
    POSIX rename. The observed failure is quoted in
    ``_data_dir_log_files_released()``'s own docstring.
    """

    @staticmethod
    def _attach(tmp_path: Path) -> tuple[logging.Logger, logging.FileHandler, Path]:
        log_file = tmp_path / "logs" / "privacyfence.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        log = logging.getLogger("privacyfence.tests.separation_release")
        log.addHandler(handler)
        log.propagate = False
        return log, handler, log_file

    def test_finds_a_handler_under_the_data_dir(self, tmp_path):
        log, handler, _ = self._attach(tmp_path)
        try:
            assert handler in privilege_separation._log_handlers_under(tmp_path)
        finally:
            log.removeHandler(handler)
            handler.close()

    def test_ignores_a_handler_outside_the_data_dir(self, tmp_path):
        log, handler, _ = self._attach(tmp_path / "inside")
        try:
            assert handler not in privilege_separation._log_handlers_under(tmp_path / "elsewhere")
        finally:
            log.removeHandler(handler)
            handler.close()

    def test_the_file_is_closed_for_the_duration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        log, handler, _ = self._attach(tmp_path)
        try:
            log.warning("before")
            assert handler.stream is not None
            with privilege_separation._data_dir_log_files_released():
                assert handler.stream is None, "the log file is still open during the move"
        finally:
            log.removeHandler(handler)
            handler.close()

    def test_the_handler_writes_again_afterwards(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        log, handler, log_file = self._attach(tmp_path)
        try:
            with privilege_separation._data_dir_log_files_released():
                pass
            log.warning("after the attempt")
            handler.flush()
            assert "after the attempt" in log_file.read_text(encoding="utf-8")
        finally:
            log.removeHandler(handler)
            handler.close()

    def test_follows_the_data_dir_when_the_enable_took(self, tmp_path, monkeypatch):
        """The success path: the directory really moved, so the handler has
        to be pointed at where it went rather than recreating an empty log
        at a path that is no longer the install's."""
        before = tmp_path / "unseparated"
        after = tmp_path / "separated"
        current = {"dir": before}
        monkeypatch.setattr(paths, "data_dir", lambda: current["dir"])
        log, handler, _ = self._attach(before)
        try:
            with privilege_separation._data_dir_log_files_released():
                shutil.move(str(before), str(after))
                current["dir"] = after
            assert Path(handler.baseFilename) == after / "logs" / "privacyfence.log"
            log.warning("written to the separated root")
            handler.flush()
            moved = after / "logs" / "privacyfence.log"
            assert "written to the separated root" in moved.read_text(encoding="utf-8")
        finally:
            log.removeHandler(handler)
            handler.close()

    def test_detaches_a_handler_it_cannot_re_establish(self, tmp_path, monkeypatch):
        """The other success path, and the ordinary one on Windows: the
        separated root is not writable by the account this process runs as,
        so the handler is dropped instead of raising out of every later
        ``logger.info()`` -- the stderr handler ``setup_logging()`` installs
        alongside it still carries the handover message."""
        before = tmp_path / "unseparated"
        current = {"dir": before}
        monkeypatch.setattr(paths, "data_dir", lambda: current["dir"])
        log, handler, _ = self._attach(before)
        try:
            def _refuse(*_args, **_kwargs):
                raise PermissionError("the separated root is the service account's")

            monkeypatch.setattr(privilege_separation.os, "makedirs", _refuse)
            with privilege_separation._data_dir_log_files_released():
                current["dir"] = tmp_path / "separated"
            assert handler not in log.handlers
        finally:
            log.removeHandler(handler)
            handler.close()


class TestWindowsFullEnableArgv:
    """The Windows sibling of maybe_auto_enable_macos()'s AppleScript --
    UAC's own elevation, which (unlike pkexec) is always interactive, so
    plain `enable` (no -Auto, which doesn't exist on this script) already
    resolves the current user and completes both halves in one call."""

    def test_elevates_via_uac_and_runs_plain_enable(self, tmp_path):
        script = tmp_path / "privilege-separation.ps1"
        argv = privilege_separation._windows_full_enable_argv(script, tmp_path / "elevated.log")

        assert "-Verb" in argv[-1] and "RunAs" in argv[-1]
        assert str(script) in argv[-1]
        assert "enable" in argv[-1]
        assert "-Auto" not in argv[-1]
        assert "-ForUser" not in argv[-1]


class TestRunFullAutoEnableNonMacos:
    @pytest.fixture
    def script(self, tmp_path, monkeypatch):
        path = tmp_path / "privilege-separation"
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: path)
        monkeypatch.setattr(privilege_separation, "_elevation_script_problem", lambda s: None)
        return path

    @pytest.fixture(autouse=True)
    def the_enable_took(self, monkeypatch):
        """The ordinary outcome: the install is separated afterwards.

        Default rather than per-test for the same reason as
        TestCompletePerUserSeparation's own: since
        privacyfence/privacyfence#599 the success *log line* is written off
        the machine rather than off an exit code, so every test below that
        expects one depends on this. The test that cares about its absence
        overrides it."""
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)

    def test_warns_when_no_script_is_found(self, monkeypatch, caplog):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: None)

        with caplog.at_level("WARNING"):
            privilege_separation._run_full_auto_enable_non_macos()
        assert "no privilege-separation provisioning script" in caplog.text

    def test_refuses_a_script_that_fails_its_safety_check(self, monkeypatch, script, caplog):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation, "_elevation_script_problem", lambda s: "owned by uid 501")

        with caplog.at_level("WARNING"):
            privilege_separation._run_full_auto_enable_non_macos()
        assert "owned by uid 501" in caplog.text

    def test_linux_without_pkexec_logs_the_manual_command(self, monkeypatch, script, caplog):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: None)

        with caplog.at_level("WARNING"):
            privilege_separation._run_full_auto_enable_non_macos()
        assert "pkexec" in caplog.text
        assert str(script) in caplog.text

    def test_linux_with_pkexec_runs_enable_auto(self, monkeypatch, script):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: "/usr/bin/pkexec")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(privilege_separation.subprocess, "run", fake_run)
        reset_calls = []
        monkeypatch.setattr(privilege_separation, "reset_cache", lambda: reset_calls.append(True))

        privilege_separation._run_full_auto_enable_non_macos()

        assert len(calls) == 1
        assert calls[0] == ["/usr/bin/pkexec", str(script), "enable", "--auto"]
        # Twice, and both are load-bearing:
        # _data_dir_log_files_released() resets on its way out so
        # paths.data_dir() re-resolves against the directory the enable has
        # just moved, and this function resets again before reading
        # is_enabled() for its own log line.
        assert reset_calls == [True, True]

    def test_windows_runs_the_uac_elevated_argv(self, monkeypatch, script):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(privilege_separation.subprocess, "run", fake_run)
        reset_calls = []
        monkeypatch.setattr(privilege_separation, "reset_cache", lambda: reset_calls.append(True))

        privilege_separation._run_full_auto_enable_non_macos()

        assert len(calls) == 1
        assert str(script) in calls[0][-1]
        assert reset_calls == [True, True]  # see the Linux case above

    def test_a_declined_prompt_is_not_a_crash(self, monkeypatch, script, caplog):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: "/usr/bin/pkexec")
        monkeypatch.setattr(
            privilege_separation.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "User cancelled."),
        )

        with caplog.at_level("INFO"):
            privilege_separation._run_full_auto_enable_non_macos()
        assert "User cancelled." in caplog.text

    def test_a_subprocess_failure_is_swallowed(self, monkeypatch, script):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: "/usr/bin/pkexec")

        def _boom(argv, **kwargs):
            raise OSError("no such binary")

        monkeypatch.setattr(privilege_separation.subprocess, "run", _boom)

        privilege_separation._run_full_auto_enable_non_macos()  # must not raise

    def test_an_exit_zero_that_separated_nothing_is_not_logged_as_success(
        self, monkeypatch, script, caplog
    ):
        # privacyfence/privacyfence#599's first defect, and the one that made
        # the rest of it undiagnosable: "privilege separation enabled
        # automatically" was written at the exact moment separation had not
        # happened, and it is the only account a user or an operator gets of a
        # start that then refuses to serve.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: "/usr/bin/pkexec")
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: False)
        monkeypatch.setattr(
            privilege_separation.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        )

        with caplog.at_level("INFO"):
            privilege_separation._run_full_auto_enable_non_macos()

        assert "enabled automatically" not in caplog.text
        assert "still not separated" in caplog.text

    def test_windows_logs_what_the_elevated_run_said(self, monkeypatch, script, caplog):
        # The second defect: a -Verb RunAs child gets its own console, so its
        # output reaches neither the parent's stdout nor its stderr. Without
        # the transcript file the only thing left to report was an exit code
        # that, on Windows, was not even the script's.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: False)

        def _run(argv, **kwargs):
            # Stand in for the elevated PowerShell, which writes the file the
            # argv above names rather than anything the parent can capture.
            transcript = Path(argv[-1].split("-LiteralPath ''", 1)[1].split("''", 1)[0])
            transcript.write_text(
                "-> creating the PrivacyFence service\n"
                "sc.exe create failed (exit 1332): No mapping between account names\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(privilege_separation.subprocess, "run", _run)

        with caplog.at_level("INFO"):
            privilege_separation._run_full_auto_enable_non_macos()

        assert "No mapping between account names" in caplog.text

    def test_the_transcript_is_removed_afterwards(self, monkeypatch, script):
        # It lives in the daemon's own temp and can hold whatever the elevated
        # run printed; nothing needs it once it has been read back into the
        # log.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        seen: list[Path] = []

        def _run(argv, **kwargs):
            seen.append(Path(argv[-1].split("-LiteralPath ''", 1)[1].split("''", 1)[0]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(privilege_separation.subprocess, "run", _run)
        monkeypatch.setattr(privilege_separation, "reset_cache", lambda: None)

        privilege_separation._run_full_auto_enable_non_macos()

        assert seen and not seen[0].exists() and not seen[0].parent.exists()


class TestElevationTranscript:
    def test_the_file_exists_before_the_elevated_process_is_started(self):
        # Created by this (unelevated) process on purpose: a file an elevated
        # child creates under %TEMP% would normally be readable anyway, and
        # "normally" is doing real work in that sentence.
        with privilege_separation._elevation_transcript() as transcript:
            assert transcript.exists()

    def test_an_unreadable_transcript_is_not_an_error(self, tmp_path):
        # This is only ever read on a path where something has already gone
        # wrong; a transcript that cannot be read is one more thing to report,
        # not a second failure.
        assert privilege_separation._elevation_transcript_text(tmp_path / "gone.log") == ""
        assert privilege_separation._elevation_transcript_text(None) == ""

    def test_a_long_transcript_keeps_its_tail(self, tmp_path):
        # The failure is at the end: the script prints its progress as it goes
        # and dies on whichever step it got to.
        transcript = tmp_path / "long.log"
        transcript.write_text("x" * 9000 + "sc.exe create failed", encoding="utf-8")

        text = privilege_separation._elevation_transcript_text(transcript, max_chars=200)

        assert text.startswith("...")
        assert text.endswith("sc.exe create failed")
        assert len(text) == 203

    def test_a_utf8_bom_does_not_reach_the_log(self, tmp_path):
        # Windows PowerShell 5.1's -Encoding utf8 means "UTF-8 with a BOM",
        # and the catch branch's -Append can put a second one mid-file.
        transcript = tmp_path / "bom.log"
        transcript.write_bytes("\ufeff-> creating the service\n\ufefffailed".encode())

        text = privilege_separation._elevation_transcript_text(transcript)

        assert text == "-> creating the service\nfailed"

    def test_the_detail_is_never_empty(self):
        # It is the whole of what a failure log line has to say, so "" would
        # leave a reader with a message that names no reason at all.
        result = subprocess.CompletedProcess(["x"], 3, "", "")

        assert privilege_separation._elevation_detail(result, None) == "no output (exit 3)"

    def test_the_detail_carries_both_sides(self, tmp_path):
        transcript = tmp_path / "t.log"
        transcript.write_text("sc.exe create failed", encoding="utf-8")
        result = subprocess.CompletedProcess(["x"], 1, "", "The operation was canceled by the user.")

        detail = privilege_separation._elevation_detail(result, transcript)

        # The launcher's own stderr says a UAC prompt was declined; the
        # transcript says what the elevated run got to. Neither substitutes
        # for the other.
        assert "canceled by the user" in detail
        assert "sc.exe create failed" in detail


class TestDevUnseparatedNotice:
    """ADR 0003 decision 7's disclosure -- consulted by daemon_main.py's
    startup log and by web/routes_security.py's local-mode /security page."""

    def test_none_on_a_packaged_build(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        privilege_separation.reset_cache()

        assert privilege_separation.dev_unseparated_notice() is None

    def test_none_when_already_separated(self, separated, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")

        assert privilege_separation.dev_unseparated_notice() is None

    def test_none_without_the_override(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        monkeypatch.delenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, raising=False)
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        privilege_separation.reset_cache()

        assert privilege_separation.dev_unseparated_notice() is None

    def test_present_for_non_packaged_unseparated_with_override(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing"))
        privilege_separation.reset_cache()

        notice = privilege_separation.dev_unseparated_notice()

        assert notice is not None
        assert privilege_separation.DEV_ALLOW_UNSEPARATED_ENV in notice
        assert "NOT protected" in notice
