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
import os
import re
import shlex
import stat
import sys
from pathlib import Path

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

    def test_mcp_token_stays_reachable_by_the_agent(self, separated):
        # #428: "mcp_token stays reachable by the agent. It is the agent's own
        # credential and the product doesn't work without it."
        token = mcp_auth.load_or_create_mcp_token()

        path = separated / "handoff" / "mcp_token"
        assert path.read_text(encoding="utf-8") == token
        assert stat.S_IMODE(path.stat().st_mode) == 0o640

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

    def test_ensure_handoff_file_mode_fixes_a_migrated_token(self, separated):
        # A token carried in from a pre-Phase-4 install arrives 0600; left
        # that way, the agent can never read its own credential again, and
        # load_or_create_mcp_token() reuses an existing file rather than
        # rewriting it.
        token_path = separated / "handoff" / "mcp_token"
        token_path.write_text("deadbeef", encoding="utf-8")
        token_path.chmod(0o600)

        assert mcp_auth.load_or_create_mcp_token() == "deadbeef"
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o640


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
        # approvals_url/settings_url are matched by the glob rather than
        # named, since the set grows with every page mint_bootstrap_url()
        # learns to mint a link for.
        assert server._bootstrap_url_file_name("/approvals").endswith("_url")

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

    def test_the_debian_postinst_auto_enables_on_configure(self):
        # The one place Linux's auto-enable actually gets invoked from --
        # see debian/postinst's own comment for why postinst (already root,
        # at package-configure time) can safely do this where the daemon
        # itself couldn't.
        postinst = (REPO_ROOT / "debian" / "postinst").read_text(encoding="utf-8")
        assert "privacyfence-privilege-separation enable --auto" in postinst
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
        upgrade_case = re.search(r"upgrade\|deconfigure\)(.*?);;", prerm, re.DOTALL)
        assert upgrade_case is not None, "no `upgrade|deconfigure)` case in debian/prerm"
        assert "privacyfence-privilege-separation" not in upgrade_case.group(1)


class TestAutoEnableMacos:
    """#428 D1 (4.1): the daemon's own trigger for auto-enabling privilege
    separation on macOS, since there's no package-manager postinst there to
    lean on the way Linux's .deb has. Nothing here can exercise the real
    ``osascript`` admin prompt (no macOS, no human to answer it) -- these
    cover the two things CI can prove: that the trigger fires (or correctly
    doesn't) under every precondition, and that the elevated command it
    builds is what it should be."""

    pytestmark = posix_permissions_only

    @staticmethod
    def _fake_thread_class(started: list):
        class _FakeThread:
            def __init__(self, **kw):
                self.kw = kw

            def start(self) -> None:
                started.append(self.kw)

        return _FakeThread

    def test_noop_on_linux(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        privilege_separation.reset_cache()
        started = []
        monkeypatch.setattr(privilege_separation.threading, "Thread", self._fake_thread_class(started))

        privilege_separation.maybe_auto_enable_macos()

        assert started == []

    def test_noop_when_already_separated(self, separated, monkeypatch):
        # separated is platform-parametrized (darwin and linux); running on
        # both proves the linux side is *also* a noop here, for the same
        # "already enabled" reason rather than the platform check above.
        started = []
        monkeypatch.setattr(privilege_separation.threading, "Thread", self._fake_thread_class(started))

        privilege_separation.maybe_auto_enable_macos()

        assert started == []

    def test_noop_without_a_packaged_or_checkout_script(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()
        monkeypatch.setattr(privilege_separation, "_macos_installer_script_path", lambda: None)
        started = []
        monkeypatch.setattr(privilege_separation.threading, "Thread", self._fake_thread_class(started))

        privilege_separation.maybe_auto_enable_macos()

        assert started == []

    def test_writes_a_marker_and_starts_the_prompt_exactly_once(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()
        script = tmp_path / "macos_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(privilege_separation, "_macos_installer_script_path", lambda: script)
        # B2's script-safety check is covered by its own TestMacosAutoEnableScriptProblem
        # below; a script this test writes itself is never root-owned, so it is bypassed
        # here to keep this test about the marker/threading behavior alone.
        monkeypatch.setattr(privilege_separation, "_macos_auto_enable_script_problem", lambda _script: None)
        data_dir = tmp_path / "data"
        monkeypatch.setattr(paths, "data_dir", lambda: data_dir)
        started = []
        monkeypatch.setattr(privilege_separation.threading, "Thread", self._fake_thread_class(started))

        privilege_separation.maybe_auto_enable_macos()

        marker = data_dir / privilege_separation.AUTO_ENABLE_ATTEMPTED_MARKER_NAME
        assert marker.is_file()
        assert len(started) == 1
        assert started[0]["args"] == (script,)

        # A second call -- the next daemon start -- must not ask again,
        # whether the human approved the first prompt or cancelled it.
        privilege_separation.maybe_auto_enable_macos()
        assert len(started) == 1

    def test_marker_write_failure_is_swallowed_not_raised(self, monkeypatch, tmp_path):
        # Best-effort like every other permission-adjacent write in this
        # module: a daemon startup path must never crash because it could
        # not write a one-byte marker file.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()
        script = tmp_path / "macos_privilege_separation.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(privilege_separation, "_macos_installer_script_path", lambda: script)
        monkeypatch.setattr(privilege_separation, "_macos_auto_enable_script_problem", lambda _script: None)
        # A file where the marker's parent directory should be: mkdir(parents=True,
        # exist_ok=True) on it raises FileExistsError (an OSError), since exist_ok
        # only tolerates an existing *directory*.
        data_dir = tmp_path / "data"
        data_dir.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(paths, "data_dir", lambda: data_dir)
        started = []
        monkeypatch.setattr(privilege_separation.threading, "Thread", self._fake_thread_class(started))

        privilege_separation.maybe_auto_enable_macos()

        assert started == []

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
        # a script that fails it must produce no elevation prompt, no
        # marker, just a log line naming why.
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
        data_dir = tmp_path / "data"
        monkeypatch.setattr(paths, "data_dir", lambda: data_dir)
        started = []
        monkeypatch.setattr(privilege_separation.threading, "Thread", self._fake_thread_class(started))

        privilege_separation.maybe_auto_enable_macos()

        assert started == []
        assert not (data_dir / privilege_separation.AUTO_ENABLE_ATTEMPTED_MARKER_NAME).exists()


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

    def test_the_installer_ships_the_template_the_script_renders(self):
        # The script resolves a checkout layout first and its own directory
        # second; a real install has only the latter, so a template missing
        # from [Files] makes `enable` die at Install-CompanionTask on exactly
        # the machines most likely to run it.
        inno = WINDOWS_INNO_SETUP.read_text(encoding="utf-8")

        assert r'Source: "windows\privacyfence-companion-task.xml.tmpl"; DestDir: "{app}"' in inno
        assert (WINDOWS_TEMPLATE_DIR / "privacyfence-companion-task.xml.tmpl").is_file()
        assert "privacyfence-companion-task.xml.tmpl" in self.SCRIPT

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
        disable = self.SCRIPT.split("function Invoke-Disable", 1)[1]

        assert "'/setowner', $script:OwnerUser" in disable

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
        assert "'obj=', $ServiceAccount" in script

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
