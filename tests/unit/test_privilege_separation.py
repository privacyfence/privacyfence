"""#428 Phase 4 (B5a): the macOS privilege-separation layout.

The thing under test is a *contract between three artifacts*: the Python
module that resolves paths from a marker file, the shell script that writes
that marker and provisions the layout it describes, and the two launchd
templates that start the two processes it splits apart. Nothing in this
repo's CI can run the real thing -- provisioning it needs root on a macOS
host, and the security property it buys only exists once two real OS accounts
are involved -- so these tests cover the two halves CI *can* prove: that every
path and mode decision follows from the marker exactly as intended, and that
the script and templates still agree with the module about what that marker
means.

``current_platform`` is monkeypatched to ``darwin`` rather than
``sys.platform`` itself, and ``PRIVACYFENCE_SYSTEM_ROOT`` relocates the whole
layout under ``tmp_path`` -- see those two names' own docstrings for why each
exists.
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
INSTALLER = REPO_ROOT / "scripts" / "macos_privilege_separation.sh"
TEMPLATE_DIR = REPO_ROOT / "installer" / "macos"

# Applied per class, not to the whole module: the marker parsing, the path
# resolution that follows from it and the installer/template contract are all
# pure logic worth running on every platform -- proving, among other things,
# #428 Phase 4's own claim that Windows behaves exactly as it did before this
# existed. What can't run there is anything that reads a POSIX mode or a file
# owner back off disk: Windows has neither (chmod there is the documented
# no-op secure_files.py's own docstring describes), the same known, accepted
# gap test_secure_files.py already skips for. It is also why B5c is a phase of
# its own rather than a platform leg of this one -- real NTFS ACLs are net-new
# work with no equivalent here.
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


@pytest.fixture
def separated(monkeypatch, tmp_path):
    """A provisioned separated install under ``tmp_path``, as the installer
    would leave it: the marker, the three directories, and the modes. Yields
    the system root."""
    root = tmp_path / "PrivacyFence"
    monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
    monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
    root.mkdir(parents=True)
    (root / "authority").mkdir()
    (root / privilege_separation.HANDOFF_DIR_NAME).mkdir()
    (root / privilege_separation.MARKER_FILE_NAME).write_text(
        json.dumps(
            {
                "version": privilege_separation.MARKER_VERSION,
                "platform": "darwin",
                "service_account": privilege_separation.SERVICE_ACCOUNT_NAME,
                "service_group": privilege_separation.SERVICE_GROUP_NAME,
                "owner_user": "alice",
                "enabled_at": "2026-09-16T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    root.chmod(privilege_separation.SYSTEM_ROOT_MODE)
    (root / "authority").chmod(privilege_separation.AUTHORITY_DIR_MODE)
    (root / privilege_separation.HANDOFF_DIR_NAME).chmod(privilege_separation.HANDOFF_DIR_MODE)
    privilege_separation.reset_cache()
    yield root
    privilege_separation.reset_cache()


def _write_marker(root: Path, **overrides) -> None:
    payload = {
        "version": privilege_separation.MARKER_VERSION,
        "platform": "darwin",
        "service_account": privilege_separation.SERVICE_ACCOUNT_NAME,
        "service_group": privilege_separation.SERVICE_GROUP_NAME,
        "owner_user": "alice",
        "enabled_at": "2026-09-16T00:00:00Z",
    }
    payload.update(overrides)
    root.mkdir(parents=True, exist_ok=True)
    (root / privilege_separation.MARKER_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")
    privilege_separation.reset_cache()


class TestNotSeparated:
    """The default, and the one that matters most: an install nobody has
    opted in has to behave byte-identically to how it did before this module
    existed."""

    def test_disabled_with_no_marker(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "nothing-here"))
        privilege_separation.reset_cache()

        assert privilege_separation.is_enabled() is False
        assert privilege_separation.data_dir_override() is None

    def test_disabled_on_an_unsupported_platform(self, monkeypatch):
        # B5b/B5c add "linux"/"win32" to SUPPORTED_PLATFORMS along with the
        # installers that can provision them. Until then, no marker can exist
        # there and this must not go looking for one.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()

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
    def _darwin_under_tmp(self, monkeypatch, tmp_path):
        self.root = tmp_path / "PrivacyFence"
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(self.root))
        privilege_separation.reset_cache()
        yield
        privilege_separation.reset_cache()

    def test_accepts_a_well_formed_marker(self):
        _write_marker(self.root)

        state = privilege_separation.separation()

        assert state is not None
        assert state.service_account == "_privacyfence"
        assert state.owner_user == "alice"
        # Derived from where the marker was found, never stored in it, so the
        # two can't disagree.
        assert state.data_dir == self.root
        assert state.authority_dir == self.root / "authority"
        assert state.handoff_dir == self.root / "handoff"

    def test_rejects_a_future_version(self):
        _write_marker(self.root, version=privilege_separation.MARKER_VERSION + 1)

        assert privilege_separation.separation() is None

    def test_rejects_another_platforms_marker(self, monkeypatch):
        _write_marker(self.root, platform="win32")

        assert privilege_separation.separation() is None

    def test_rejects_a_marker_missing_a_field(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / privilege_separation.MARKER_FILE_NAME).write_text(
            json.dumps({"version": 1, "platform": "darwin"}), encoding="utf-8"
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
        _write_marker(self.root)
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
    def test_ignores_a_relative_override(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, "relative/path")
        privilege_separation.reset_cache()

        # A relative root would resolve differently per process depending on
        # each one's cwd -- the daemon's comes from its LaunchDaemon, the
        # companion's from whatever launched it -- so half an install would
        # silently use a different directory.
        assert privilege_separation.system_root() == privilege_separation.MACOS_SYSTEM_ROOT

    def test_default_root_on_macos(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.delenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, raising=False)
        privilege_separation.reset_cache()

        assert privilege_separation.system_root() == Path("/Library/Application Support/PrivacyFence")


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

    def test_a_real_installs_sockets_fit_in_sun_path(self):
        # The two comparisons above are deliberately fallback-agnostic, which
        # means they would also pass if the shipped layout were too long for
        # AF_UNIX and every socket silently landed in /tmp instead. It isn't,
        # and that is a property of the directory names this phase chose rather
        # than an accident -- so assert it against the real root.
        handoff = privilege_separation.MACOS_SYSTEM_ROOT / privilege_separation.HANDOFF_DIR_NAME

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
    def test_unseparated_install_is_always_fine(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(tmp_path / "absent"))
        privilege_separation.reset_cache()

        privilege_separation.check_runtime_identity()

    def test_accepts_the_service_account(self, separated, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "_privacyfence")

        privilege_separation.check_runtime_identity()

    def test_refuses_the_logged_in_user(self, separated, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "alice")

        with pytest.raises(privilege_separation.PrivilegeSeparationError) as exc:
            privilege_separation.check_runtime_identity()
        assert "_privacyfence" in str(exc.value)

    def test_refuses_a_marker_it_could_not_parse(self, monkeypatch, tmp_path):
        # The dangerous case: paths.py would resolve the *un*separated layout
        # while a separated one sits on disk, and load_config()'s own first-run
        # behavior would seed a fresh default policy over the real one.
        root = tmp_path / "PrivacyFence"
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setenv(privilege_separation.SYSTEM_ROOT_ENV_VAR, str(root))
        _write_marker(root, version=99)

        with pytest.raises(privilege_separation.PrivilegeSeparationError):
            privilege_separation.check_runtime_identity()


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

    def test_running_as_service_account_follows_the_marker(self, separated, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_user_name", lambda: "_privacyfence")
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

    def test_an_authority_dir_the_service_account_owns_is_clean(self, separated, monkeypatch):
        # The passing case for the ownership probe: pretend this process's own
        # account *is* the service account, which is what a real separated
        # install looks like from the daemon's side.
        monkeypatch.setattr(privilege_separation, "SERVICE_ACCOUNT_NAME", this_account())
        _write_marker(separated, service_account=this_account())

        assert privilege_separation.audit_layout() == []

    def test_an_unreadable_marker_is_reported_but_not_fatal(self, monkeypatch, tmp_path):
        # ENOENT is the ordinary "not separated" answer and stays silent; any
        # other OSError means a hand-edited layout and is worth a warning.
        root = tmp_path / "PrivacyFence"
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
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
    """The shell script and this module are two halves of one contract: the
    script provisions a layout, the module resolves paths from it, and neither
    can see the other at runtime. A silent drift between them leaves a daemon
    looking for its data where the installer never put it."""

    SCRIPT = INSTALLER.read_text(encoding="utf-8")

    def _assign(self, name: str) -> str:
        match = re.search(rf'^{name}="?([^"\n]*)"?$', self.SCRIPT, re.MULTILINE)
        assert match is not None, f"{name} is not assigned in {INSTALLER.name}"
        return match.group(1)

    def test_is_executable(self):
        assert os.access(INSTALLER, os.X_OK)

    def test_account_names_match(self):
        assert self._assign("SERVICE_ACCOUNT") == privilege_separation.SERVICE_ACCOUNT_NAME
        assert self._assign("SERVICE_GROUP") == privilege_separation.SERVICE_GROUP_NAME

    def test_system_root_matches(self):
        assert self._assign("SYSTEM_ROOT") == str(privilege_separation.MACOS_SYSTEM_ROOT)

    def test_marker_matches(self):
        assert self._assign("MARKER_NAME") == privilege_separation.MARKER_FILE_NAME
        assert int(self._assign("MARKER_VERSION")) == privilege_separation.MARKER_VERSION

    def test_handoff_dir_name_matches(self):
        assert self._assign("HANDOFF_DIR_NAME") == privilege_separation.HANDOFF_DIR_NAME

    @pytest.mark.parametrize(
        "script_name,module_constant",
        [
            ("SYSTEM_ROOT_MODE", privilege_separation.SYSTEM_ROOT_MODE),
            ("HANDOFF_DIR_MODE", privilege_separation.HANDOFF_DIR_MODE),
            ("AUTHORITY_DIR_MODE", privilege_separation.AUTHORITY_DIR_MODE),
            ("HANDOFF_FILE_MODE", privilege_separation.HANDOFF_FILE_MODE_SEPARATED),
        ],
    )
    def test_modes_match(self, script_name, module_constant):
        # The script spells them as chmod arguments (711), the module as
        # Python octal literals (0o711) -- same numbers, two notations.
        assert int(self._assign(script_name), 8) == module_constant

    def test_migrates_every_file_that_moved_into_the_handoff_dir(self):
        # Each of these sits at the root of a pre-Phase-4 data directory and
        # has to end up inside handoff/, or something in the user's session
        # loses track of the daemon: the shim loses mcp_url/mcp_token, the
        # companion loses web_base_url.
        names = re.search(r"^HANDOFF_FILE_NAMES=\(([^)]*)\)$", self.SCRIPT, re.MULTILINE)
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

    @pytest.mark.parametrize("subcommand", ["enable", "disable", "status"])
    def test_documents_each_subcommand(self, subcommand):
        assert f"cmd_{subcommand}()" in self.SCRIPT

    def test_refuses_to_run_without_the_companion(self):
        # ADR 0002 decision 2: after Phase 4 nobody in the user's desktop
        # session can mint a sign-in link except the companion, so installing
        # the daemon half alone is a locked door.
        assert "a separated install needs the companion app" in self.SCRIPT


class TestLaunchdTemplates:
    DAEMON = (TEMPLATE_DIR / "com.privacyfence.daemon.plist.tmpl").read_text(encoding="utf-8")
    COMPANION = (TEMPLATE_DIR / "com.privacyfence.companion.plist.tmpl").read_text(encoding="utf-8")

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
        substituted = set(re.findall(r"-e \"s\|(__[A-Z_]+__)\|", TestInstallerContract.SCRIPT))
        used = set(re.findall(r"__[A-Z_]+__", self.DAEMON + self.COMPANION))

        assert used <= substituted, f"never substituted: {sorted(used - substituted)}"

    def test_labels_match_the_installer(self):
        assert re.search(r'^DAEMON_LABEL="com\.privacyfence\.daemon"$', TestInstallerContract.SCRIPT, re.MULTILINE)
        assert re.search(
            r'^COMPANION_LABEL="com\.privacyfence\.companion"$', TestInstallerContract.SCRIPT, re.MULTILINE
        )
