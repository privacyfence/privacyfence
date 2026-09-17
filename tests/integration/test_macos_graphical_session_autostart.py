"""Real graphical-session autostart verification for the packaged macOS ``.app``
(B19, privacyfence/privacyfence#374).

``test_macos_packaged_smoke.py`` (TST-15) already proves the DMG-packaged app starts
and serves a real MCP/approval round trip -- run as a direct subprocess, never
through launchd (its own module docstring's 7-step list has no ``launchctl``/plist
step at all). What that leaves unproven is the actual autostart wiring #428 D1 gives
every install the moment it separates: the daemon becomes a LaunchDaemon under its
own dedicated account, and what starts in the logged-in user's own session instead is
the companion, as a LaunchAgent (``scripts/macos_privilege_separation.sh``,
``installer/macos/*.plist.tmpl`` -- ADR 0002's "the startup wiring inverts").

Unlike Linux's ``.deb`` (``debian/postinst`` auto-separates at package-install time,
no prompt at all) and Windows (no B5c auto-enable yet), macOS has no package-manager
postinst to lean on -- a DMG install is a drag to ``/Applications``, nothing runs as
root at install time. So the *only* automatic path, ``privilege_separation.
maybe_auto_enable_macos()``, fires from the daemon's own first start instead and
shells out through ``osascript ... with administrator privileges`` -- a real
admin-password dialog nothing in CI can answer (see that function's own module
docstring). This module does not attempt to drive that dialog; it drives the same
script the way a human running it by hand does instead --
``sudo scripts/macos_privilege_separation.sh enable`` -- the same
passwordless-sudo substitution ``test_deb_packaged_lifecycle.py``'s own
``_can_install_packages()`` already makes, for the same reason.

A GitHub-hosted ``macos-latest`` runner's default account is already logged into a
real Aqua session (needed for Xcode/Simulator GUI work), which is what makes
``launchctl bootstrap gui/<uid> <plist>`` reachable here at all -- the daemon's own
``system/`` LaunchDaemon domain needs nothing but root, so that half was never in
question; the companion's ``gui/<uid>`` domain is the one this module actually earns.
That's deliberately as far as this goes: proving both halves of the inversion via
``launchctl`` itself, against the one real login session a CI job has -- not a second
physical login, which is out of scope here the same way it is for
``test_windows_graphical_session_autostart.py``'s own single logged-on account.

``enable`` also runs a real B1 check (``require_trusted_image()``) against whatever
``--app`` points at, walking every directory up to ``/`` and refusing to elevate
anything staged somewhere user-writable -- correctly, since a writable image is a
writable "run this as a different, more trusted account" primitive. That is exactly
what makes ``/tmp`` (where ``tempfile.mkdtemp()`` lands, and macOS's own always-
world-writable regardless of the sticky bit) the wrong place to stage the extracted
bundle from: this module first extracts the DMG into an ordinary scratch directory
(``_copy_app_from_dmg``), then promotes that copy into a fresh root:wheel-owned,
non-writable tree under ``/Library`` (``_stage_as_root``) before ever calling
``enable`` -- the same shape a genuinely trusted install needs, proven rather than
bypassed.

Proves, all for real:

- ``sudo .../macos_privilege_separation.sh enable`` leaves the LaunchDaemon
  (``system/com.privacyfence.daemon``) actually running, as ``_privacyfence``, from
  the packaged daemon binary;
- the companion LaunchAgent (``gui/<uid>/com.privacyfence.companion``) actually
  running, as the CI account (not ``_privacyfence``), from the packaged companion
  binary;
- both control channels' own functional proof, not just "launchd thinks it's
  active": the daemon's real ``control.sock`` + ``mcp_token``, and the companion's
  real ``companion.sock``, all under the separated ``handoff/`` directory
  (``web/control_channel.py``, ``privilege_separation.py``).

Deliberately does not repeat ``test_macos_packaged_smoke.py``'s own MCP/browser round
trip -- that is already proven against a directly-exec'd binary; this module's own
job is the launchd wiring around it.

Skipped entirely unless running on real macOS with a just-built DMG on disk and
passwordless sudo -- same posture as
``tests/integration/test_linux_graphical_session_autostart.py``, scheduled the same
way via its own ``.github/workflows/macos-graphical-session.yml`` (packaging-related
``main`` pushes, weekly, manual dispatch -- never tag-gating a release; see that
workflow's own docstring and this repo's ``CLAUDE.md``).
"""
from __future__ import annotations

import getpass
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from privacyfence.privilege_separation import (
    HANDOFF_DIR_NAME,
    MACOS_SERVICE_ACCOUNT_NAME,
    MACOS_SYSTEM_ROOT,
    MARKER_FILE_NAME,
)
from privacyfence.web.control_channel import companion_socket_path_under, socket_path_under
from tests.diagnostics import failure_dir, suite_name_for, write_environment_info

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "dist"
PRIVILEGE_SEPARATION_SCRIPT = REPO_ROOT / "scripts" / "macos_privilege_separation.sh"

DAEMON_LABEL = "com.privacyfence.daemon"
COMPANION_LABEL = "com.privacyfence.companion"
MCP_TOKEN_FILE_NAME = "mcp_token"  # web/mcp_auth.py's MCP_TOKEN_FILE_NAME

MARKER_PATH = MACOS_SYSTEM_ROOT / MARKER_FILE_NAME
HANDOFF_DIR = MACOS_SYSTEM_ROOT / HANDOFF_DIR_NAME


def _built_dmgs() -> list[Path]:
    return sorted(DIST_DIR.glob("PrivacyFence-*.dmg")) if DIST_DIR.is_dir() else []


def _copy_app_from_dmg(dst_dir: Path) -> Path:
    """Mounts the just-built DMG and copies ``PrivacyFenceApp.app`` out of it
    into ``dst_dir`` -- the "install" step, without writing to this runner's
    real ``/Applications``. Deliberately not imported from
    ``test_macos_packaged_smoke.py``: that module's own top level runs
    ``pytest.importorskip("mcp"/"playwright...")``, which this module has no
    reason to depend on just to reuse ~15 lines -- same "stay independently
    runnable" posture that module's own ``built_shim_entry`` fixture already
    states for itself.

    Lands under a plain user-owned scratch directory -- this is only ever the
    *extraction* step. ``_stage_as_root`` below is what produces a path
    ``macos_privilege_separation.sh`` will actually accept."""
    dmg_path = _built_dmgs()[-1]
    mount_point = Path(tempfile.mkdtemp(prefix="pf-dmg-mount-"))
    subprocess.run(
        ["hdiutil", "attach", str(dmg_path), "-nobrowse", "-readonly", "-mountpoint", str(mount_point)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    try:
        app_src = mount_point / "PrivacyFenceApp.app"
        assert app_src.is_dir(), f"PrivacyFenceApp.app missing from {dmg_path} (mounted at {mount_point})"
        app_dst = dst_dir / "PrivacyFenceApp.app"
        shutil.copytree(app_src, app_dst, symlinks=True)
        return app_dst
    finally:
        subprocess.run(["hdiutil", "detach", str(mount_point), "-force"], capture_output=True, text=True, timeout=30)
        shutil.rmtree(mount_point, ignore_errors=True)


# B1 (ADR 0002): macos_privilege_separation.sh's own require_trusted_image() walks the daemon/
# companion executable's path *and every directory above it, up to "/"*, refusing to elevate
# anything unless each one is root-owned and not world/group-writable (group "wheel" excepted).
# /tmp (what tempfile.mkdtemp() -- and this module's own app_dir fixture -- lands under) is always
# world-writable on macOS, sticky bit or not, so nothing staged there can ever pass that walk no
# matter how the leaf directory itself is chmod'd. /Library is the parent every existing
# MACOS_SYSTEM_ROOT write already trusts (the daemon's own "/Library/Application Support/
# PrivacyFence" lives right under it), so it's the natural, already-safe place to stage from here
# too, rather than inventing a new top-level path.
_ROOT_STAGING_PARENT = Path("/Library")


def _stage_as_root(app_path: Path) -> Path:
    """Copies ``app_path`` (freshly extracted from the DMG into a user-owned scratch dir) into a
    fresh, root:wheel-owned, non-group/world-writable directory under ``_ROOT_STAGING_PARENT`` --
    the shape ``require_trusted_image()`` actually accepts, see the module-level comment above.
    Every copy/chown/chmod goes through ``sudo`` since the destination is never writable by this
    test's own (non-root) uid once it exists. Caller owns removing the returned tree (via
    ``_remove_root_owned``) once the test is done with it."""
    staging_dir = _ROOT_STAGING_PARENT / f"pf-graphical-session-test-{os.getpid()}"
    _sudo_run("rm", "-rf", str(staging_dir), check=False)
    _sudo_run("mkdir", "-p", str(staging_dir))
    staged_app = staging_dir / app_path.name
    _sudo_run("cp", "-R", str(app_path), str(staged_app))
    # Recursive chown covers both directories and files in one pass; chmod afterwards so nothing
    # in between is briefly group/world-writable under the new root ownership. 755 everywhere is
    # coarser than a real signed bundle's own per-file modes, but this workflow never signs
    # (scripts/build_dmg.sh runs with no --sign here) and require_trusted_image() only cares about
    # "not writable by anyone but root", never execute bits on non-executables.
    _sudo_run("chown", "-R", "root:wheel", str(staging_dir))
    _sudo_run("chmod", "-R", "755", str(staging_dir))
    return staged_app


def _remove_root_owned(path: Path) -> None:
    _sudo_run("rm", "-rf", str(path), check=False)


def _can_sudo() -> bool:
    """Same passwordless-sudo probe ``test_deb_packaged_lifecycle.py``'s own
    ``_can_install_packages()`` makes, for the same reason: creating a
    system account and a LaunchDaemon both need root, and this fails fast
    rather than hanging on a password prompt nothing in CI will ever
    answer."""
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _current_user() -> str:
    return getpass.getuser()


pytestmark = [
    pytest.mark.packaged,
    pytest.mark.skipif(platform.system() != "Darwin", reason="only meaningful on real macOS"),
    pytest.mark.skipif(
        not _built_dmgs(),
        reason=(
            "no dist/PrivacyFence-*.dmg built yet -- run scripts/build_dmg.sh first, same as "
            "test_macos_packaged_smoke.py's own identical skip"
        ),
    ),
    pytest.mark.skipif(
        not _can_sudo(), reason="enabling privilege separation needs root -- run as root or with passwordless sudo",
    ),
    pytest.mark.skipif(shutil.which("launchctl") is None, reason="launchctl not on PATH"),
    # A real DMG mount/copy, a real `enable` (system account creation, data-layout
    # provisioning, two real launchd bootstraps) and polling two real processes to
    # actually come up -- same heavy-setup timeout reasoning as every other
    # packaged/system test in this repo.
    pytest.mark.timeout(180),
]


# --------------------------------------------------------------------------- #
# launchctl/sudo helpers
# --------------------------------------------------------------------------- #

def _sudo_run(*args: str, check: bool = True, timeout: float = 30) -> subprocess.CompletedProcess:
    result = subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)
    if check:
        assert result.returncode == 0, f"sudo {' '.join(args)} failed:\n{result.stdout}{result.stderr}"
    return result


def _sudo_path_exists(path: Path) -> bool:
    """Existence check via ``sudo -n test -e`` -- same reasoning as the
    Linux graphical-session module's own helper of the same name: these
    paths sit under a 0711/0700/2770 tree owned by ``_privacyfence``, and
    this test's own CI account only picks up its new group membership on
    its *next* login (``cmd_enable``'s own printed note), which this
    already-running process never gets."""
    return subprocess.run(["sudo", "-n", "test", "-e", str(path)], capture_output=True, timeout=10).returncode == 0


def _wait_for_path_as_root(path: Path, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _sudo_path_exists(path):
            return
        time.sleep(0.1)
    raise AssertionError(f"{what} ({path}) never appeared within {timeout}s")


def _launchctl_print(domain: str) -> str | None:
    """``launchctl print <domain>``'s stdout, or None if the job isn't
    loaded at all -- via ``sudo -n`` throughout (newer macOS refuses even a
    read-only ``print`` of the ``system/`` domain to a non-root caller;
    running every check the same way keeps the daemon/companion assertions
    symmetric instead of one needing root and the other not)."""
    result = _sudo_run("launchctl", "print", domain, check=False)
    return result.stdout if result.returncode == 0 else None


def _wait_for_running(domain: str, *, timeout: float) -> str:
    """Polls ``launchctl print <domain>`` until it reports a real pid --
    this module's own analogue of the Linux test's
    ``_wait_for_unit_property`` (``ActiveState=active``) and the Windows
    test's task-state poll: ``RunAtLoad`` starts the job the moment
    ``bootstrap`` succeeds, but not synchronously with that call
    returning."""
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        last = _launchctl_print(domain)
        if last is not None:
            match = re.search(r"^\s*pid\s*=\s*(\d+)", last, re.MULTILINE)
            if match:
                return match.group(1)
        time.sleep(0.2)
    raise AssertionError(f"{domain} never reported a running pid within {timeout}s:\n{last}")


def _process_owner(pid: str) -> str:
    return subprocess.run(["ps", "-o", "user=", "-p", pid], capture_output=True, text=True, timeout=10).stdout.strip()


def _process_command(pid: str) -> str:
    return subprocess.run(["ps", "-o", "comm=", "-p", pid], capture_output=True, text=True, timeout=10).stdout.strip()


# --------------------------------------------------------------------------- #
# Diagnostics + cleanup
# --------------------------------------------------------------------------- #

def _capture_separation_diagnostics(request, uid: int) -> None:
    """This module's own small capture call (docs/coding-and-testing-
    guidelines.md's "System/packaged-artifact test diagnostics" section):
    ``enable`` drives a real system-wide install, not something a
    ``tmp_path`` isolates, so the generic per-``tmp_path`` manifest
    ``tests/diagnostics.py`` gives every other packaged test for free finds
    nothing here. Reads everything through ``sudo`` for the same reason
    every assertion above does."""
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call is None or not rep_call.failed:
        return
    dest = failure_dir(request.node.nodeid, suite=suite_name_for(__file__))
    dest.mkdir(parents=True, exist_ok=True)
    write_environment_info(dest / "environment.txt")

    status = _sudo_run(str(PRIVILEGE_SEPARATION_SCRIPT), "status", "--user", _current_user(), check=False)
    (dest / "privilege-separation-status.txt").write_text(status.stdout + status.stderr, encoding="utf-8")

    for domain, name in ((f"system/{DAEMON_LABEL}", "daemon"), (f"gui/{uid}/{COMPANION_LABEL}", "companion")):
        printed = _launchctl_print(domain)
        (dest / f"launchctl-print-{name}.txt").write_text(printed or "(not loaded)", encoding="utf-8")

    listing = _sudo_run("find", str(MACOS_SYSTEM_ROOT), check=False)
    (dest / "system-root-manifest.txt").write_text(listing.stdout + listing.stderr, encoding="utf-8")

    daemon_log = _sudo_run("cat", str(MACOS_SYSTEM_ROOT / "logs" / "launchd.log"), check=False)
    (dest / "daemon-launchd.log").write_text(daemon_log.stdout + daemon_log.stderr, encoding="utf-8")


def _disable_if_separated() -> None:
    if _sudo_path_exists(MARKER_PATH):
        _sudo_run(str(PRIVILEGE_SEPARATION_SCRIPT), "disable", "--user", _current_user(), check=False, timeout=60)


@pytest.fixture
def _clean_separation_state(request):
    """Every test in this module drives ``enable``/``disable`` against real
    machine-wide state (a system account, a LaunchDaemon, a LaunchAgent) --
    global state a ``tmp_path`` cannot isolate, the same reason
    ``test_deb_packaged_lifecycle.py``'s own ``_clean_package_state``
    guarantees a clean slate on both sides. Guaranteed at the start too, in
    case a previous, interrupted run never reached its own teardown."""
    _disable_if_separated()
    try:
        yield
    finally:
        _capture_separation_diagnostics(request, os.getuid())
        _disable_if_separated()


# --------------------------------------------------------------------------- #
# The test
# --------------------------------------------------------------------------- #

def test_macos_privilege_separation_wires_daemon_and_companion_autostart(_clean_separation_state):
    app_dir = Path(tempfile.mkdtemp(prefix="pf-graphical-session-"))
    staged_app: Path | None = None
    try:
        extracted_app = _copy_app_from_dmg(app_dir)
        # require_trusted_image() (B1) refuses to elevate anything staged under a user-writable
        # path -- see _stage_as_root's own comment -- so this promotes the extracted bundle to a
        # root-owned tree before handing it to `enable`, the same way a real trusted install would
        # need to already be laid out.
        staged_app = _stage_as_root(extracted_app)
        app_path = staged_app
        user = _current_user()
        uid = os.getuid()

        enable = _sudo_run(str(PRIVILEGE_SEPARATION_SCRIPT), "enable", "--app", str(app_path), "--user", user, timeout=60)
        assert _sudo_path_exists(MARKER_PATH), f"{MARKER_PATH} missing after enable:\n{enable.stdout}{enable.stderr}"

        # ── The daemon: a LaunchDaemon in the system/ domain, running as the
        # dedicated service account -- no login session involved at all. ──
        daemon_pid = _wait_for_running(f"system/{DAEMON_LABEL}", timeout=20)
        daemon_owner = _process_owner(daemon_pid)
        assert daemon_owner == MACOS_SERVICE_ACCOUNT_NAME, (
            f"{DAEMON_LABEL} (pid {daemon_pid}) should run as {MACOS_SERVICE_ACCOUNT_NAME}, not {daemon_owner!r}"
        )
        daemon_command = _process_command(daemon_pid)
        assert daemon_command.endswith("PrivacyFenceApp"), (
            f"{DAEMON_LABEL} (pid {daemon_pid}) is not running the packaged daemon binary: {daemon_command!r}"
        )

        # Functional proof, not just "launchd thinks it's active": the
        # daemon actually reached the point of writing its own control
        # socket and minting mcp_token -- privilege_separation.py's own
        # handoff/ contract.
        _wait_for_path_as_root(
            socket_path_under(HANDOFF_DIR), timeout=20, what="the separated daemon's control channel socket",
        )
        _wait_for_path_as_root(HANDOFF_DIR / MCP_TOKEN_FILE_NAME, timeout=20, what="the separated daemon's mcp_token")

        # ── The companion: a LaunchAgent bootstrapped straight into this
        # runner's own already-logged-in GUI session (gui/<uid>) -- the
        # exact call a real human's second login would otherwise be needed
        # to prove; a GitHub-hosted macos-latest runner already has a real
        # Aqua session for its default user, which is what makes this
        # reachable here at all (module docstring). ───────────────────────
        companion_domain = f"gui/{uid}/{COMPANION_LABEL}"
        companion_pid = _wait_for_running(companion_domain, timeout=20)
        companion_owner = _process_owner(companion_pid)
        assert companion_owner == user, (
            f"{COMPANION_LABEL} (pid {companion_pid}) should run as {user!r} (the logged-in human), "
            f"not {companion_owner!r}"
        )
        companion_command = _process_command(companion_pid)
        assert companion_command.endswith("PrivacyFenceCompanion"), (
            f"{COMPANION_LABEL} (pid {companion_pid}) is not running the packaged companion binary: "
            f"{companion_command!r}"
        )

        _wait_for_path_as_root(
            companion_socket_path_under(HANDOFF_DIR), timeout=20, what="the companion's own control channel socket",
        )
    finally:
        if staged_app is not None:
            _remove_root_owned(staged_app.parent)
        shutil.rmtree(app_dir, ignore_errors=True)
