"""#428 Phase 4 (B5a/B5b): the macOS and Linux privilege-separation layouts.

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

Everything that isn't a name is shared between the two platforms, which is
why almost every test here runs against both: what B5b added to B5a is three
strings (a root, an account, a group) and a second installer, and a test
that only ever exercised one platform's strings would not have noticed the
other's going wrong.

``current_platform`` is monkeypatched rather than ``sys.platform`` itself,
and ``PRIVACYFENCE_SYSTEM_ROOT`` relocates the whole layout under
``tmp_path`` -- see those two names' own docstrings for why each exists.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from privacyfence import paths, privilege_separation, secure_files
from privacyfence.web import control_channel, mcp_auth

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every platform #428 P4 has shipped for, and the installer that provisions
# it. Kept as its own table rather than derived from PLATFORM_LAYOUTS so a
# platform added to the module without an installer fails here loudly --
# which is exactly the mistake PLATFORM_LAYOUTS' own comment warns about.
PLATFORMS = ("darwin", "linux")
INSTALLERS = {
    "darwin": REPO_ROOT / "scripts" / "macos_privilege_separation.sh",
    "linux": REPO_ROOT / "scripts" / "linux_privilege_separation.sh",
}
MACOS_TEMPLATE_DIR = REPO_ROOT / "installer" / "macos"
LINUX_TEMPLATE_DIR = REPO_ROOT / "installer" / "linux"
SHIM_PROTOCOL = REPO_ROOT / "mcpb" / "shim" / "src" / "protocol.ts"

# Applied per class, not to the whole module: the marker parsing, the path
# resolution that follows from it and the installer/template contract are all
# pure logic worth running on every platform -- proving, among other things,
# #428 Phase 4's own claim that Windows behaves exactly as it did before this
# existed. What can't run there is anything that reads a POSIX mode or a file
# owner back off disk: Windows has neither (chmod there is the documented
# no-op secure_files.py's own docstring describes), the same known, accepted
# gap test_secure_files.py already skips for. It is also why B5c is a phase of
# its own rather than a platform leg of these two -- real NTFS ACLs are
# net-new work with no equivalent here.
posix_permissions_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="reads POSIX ownership/permission bits back off disk -- Windows has none, and #428 P4's Windows phase (B5c) is NTFS ACLs rather than this",
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
        # B5c adds "win32" to PLATFORM_LAYOUTS along with the installer that
        # can provision it. Until then, no marker can exist there and this
        # must not go looking for one.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
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
        ],
    )
    def test_default_root_per_platform(self, platform, expected, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: platform)
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()

        assert privilege_separation.system_root() == Path(expected)

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

    @pytest.mark.parametrize("platform", PLATFORMS)
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


class TestAuditLayout:
    pytestmark = posix_permissions_only

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
    """Each platform's shell script and this module are two halves of one
    contract: the script provisions a layout, the module resolves paths from
    it, and neither can see the other at runtime. A silent drift between them
    leaves a daemon looking for its data where the installer never put it.

    Everything here runs against every shipped platform's installer, because
    the two scripts share every constant except the three names -- and a
    check that only ever read one of them would not have caught the other
    drifting."""

    SCRIPTS = {platform: path.read_text(encoding="utf-8") for platform, path in INSTALLERS.items()}

    def _assign(self, platform: str, name: str) -> str:
        match = re.search(rf'^{name}="?([^"\n]*)"?$', self.SCRIPTS[platform], re.MULTILINE)
        assert match is not None, f"{name} is not assigned in {INSTALLERS[platform].name}"
        return match.group(1)

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_is_executable(self, platform):
        assert os.access(INSTALLERS[platform], os.X_OK)

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_account_names_match(self, platform):
        layout = privilege_separation.PLATFORM_LAYOUTS[platform]
        assert self._assign(platform, "SERVICE_ACCOUNT") == layout.service_account
        assert self._assign(platform, "SERVICE_GROUP") == layout.service_group

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_system_root_matches(self, platform):
        # as_posix(), not str(): this file is collected on Windows too, where
        # Path is a WindowsPath and stringifies the very same constant with
        # backslashes. The scripts' values are POSIX paths by nature, so that
        # spelling is the one both sides actually mean.
        expected = privilege_separation.PLATFORM_LAYOUTS[platform].system_root
        assert self._assign(platform, "SYSTEM_ROOT") == expected.as_posix()

    @pytest.mark.parametrize("platform", PLATFORMS)
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

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_marker_matches(self, platform):
        assert self._assign(platform, "MARKER_NAME") == privilege_separation.MARKER_FILE_NAME
        assert int(self._assign(platform, "MARKER_VERSION")) == privilege_separation.MARKER_VERSION

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_writes_its_own_platform_into_the_marker(self, platform):
        # separation() rejects a marker whose platform isn't this one, so a
        # script writing the wrong string would produce an install that every
        # process reads as un-separated while a separated layout sits on disk
        # -- check_runtime_identity()'s worst case.
        assert f'"platform": "{platform}"' in self.SCRIPTS[platform]

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_handoff_dir_name_matches(self, platform):
        assert self._assign(platform, "HANDOFF_DIR_NAME") == privilege_separation.HANDOFF_DIR_NAME

    @pytest.mark.parametrize("platform", PLATFORMS)
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

    @pytest.mark.parametrize("platform", PLATFORMS)
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

    @pytest.mark.parametrize("platform", PLATFORMS)
    @pytest.mark.parametrize("subcommand", ["enable", "disable", "status"])
    def test_documents_each_subcommand(self, platform, subcommand):
        assert f"cmd_{subcommand}()" in self.SCRIPTS[platform]

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_refuses_to_run_without_the_companion(self, platform):
        # ADR 0002 decision 2: after Phase 4 nobody in the user's desktop
        # session can mint a sign-in link except the companion, so installing
        # the daemon half alone is a locked door.
        assert "a separated install needs the companion app" in self.SCRIPTS[platform]

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_refuses_to_run_on_the_other_platform(self, platform):
        # Both scripts do the same chown/chmod/account work on paths that
        # exist under both OSes; running the wrong one would half-provision a
        # layout at a root nothing reads.
        assert "uname -s" in self.SCRIPTS[platform]


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
