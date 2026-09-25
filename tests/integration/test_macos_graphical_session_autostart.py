"""Real graphical-session autostart verification for the packaged macOS ``.app``.

``test_macos_packaged_smoke.py`` already proves the DMG-packaged app starts
and serves a real MCP/approval round trip -- run as a direct subprocess, never
through launchd (its own module docstring's 7-step list has no ``launchctl``/plist
step at all). What that leaves unproven is the actual autostart wiring ADR 0003 gives
every install the moment it separates: the daemon becomes a LaunchDaemon under its
own dedicated account, and what starts in the logged-in user's own session instead is
the companion, as a LaunchAgent (``scripts/macos_privilege_separation.sh``,
``installer/macos/*.plist.tmpl`` -- ADR 0002's "the startup wiring inverts").

macOS's own package-manager-equivalent path -- the ``.pkg``'s ``postinstall`` running
as root at install time, the way Linux's ``.deb`` does it with ``debian/postinst`` --
is ``test_macos_pkg_install.py``'s subject, in this same workflow. What this module
covers is the other way in, still very much reachable: ``privilege_separation.
maybe_auto_enable_macos()``, which fires from the daemon's own first start for an
install that never went through the installer, and shells out through ``osascript ...
with administrator privileges`` -- a real admin-password dialog nothing in CI can
answer (see that function's own module docstring). This module does not attempt to
drive that dialog; it drives the same script the way a human running it by hand does
instead -- ``sudo scripts/macos_privilege_separation.sh enable`` -- the same
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

``enable`` also runs a real trusted-image check (``require_trusted_image()``), but not
against whatever ``--app`` points at directly: walking every directory up to ``/`` from
a real ``/Applications/PrivacyFenceApp.app`` always fails it, since ``/Applications``
itself is admin-group-writable on every real Mac -- not a CI-only quirk, and it applies
to the *daemon's own* runtime auto-enable prompt (``maybe_auto_enable_macos()``) just
the same. So ``enable`` stages its own root:wheel-owned copy of whatever ``--app``
points at (``stage_trusted_image()``, into ``TRUSTED_IMAGE_DIR``) before trusting
anything, and checks *that* copy instead -- so this module hands it a plain,
``/tmp``-extracted, user-owned copy directly (``_copy_app_from_dmg``), the least
privileged shape an unseparated install can have, rather than pre-staging a trusted
one itself. The
daemon/companion assertions below confirm the running processes actually come from
``TRUSTED_IMAGE_DIR``, not just that a same-named binary is running from somewhere.

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
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

from privacyfence.privilege_separation import (
    HANDOFF_DIR_NAME,
    MACOS_SERVICE_ACCOUNT_NAME,
    MACOS_SYSTEM_ROOT,
    MARKER_FILE_NAME,
)
from privacyfence.companion import _STATUS_POLL_SECONDS
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
# ADR 0008 D3: the owner's own mcp_token lives under the *authority* root,
# not handoff/ -- handoff/ is unaffected (still the control channel socket,
# companion socket, and web_base_url).
AUTHORITY_DIR = MACOS_SYSTEM_ROOT / "authority"


def _built_dmgs() -> list[Path]:
    return sorted(DIST_DIR.glob("PrivacyFence-*.dmg")) if DIST_DIR.is_dir() else []


def _copy_app_from_dmg(dst_dir: Path) -> Path:
    """Mounts the just-built DMG, expands the ``PrivacyFence.pkg`` it carries
    and copies ``PrivacyFenceApp.app`` out of that package's payload into
    ``dst_dir`` -- the "install" step, without writing to this runner's real
    ``/Applications`` (and without running the package's own ``postinstall``,
    which is exactly what ``test_macos_pkg_install.py`` exists to do instead).
    The payload is where the app bundle lives in a shipped artifact at all:
    the DMG carries the installer and the ``.mcpb``, not a draggable bundle --
    see ``scripts/build_dmg.sh``.

    Deliberately not imported from ``test_macos_packaged_smoke.py``: that
    module's own top level runs ``pytest.importorskip("mcp"/"playwright...")``,
    which this module has no reason to depend on just to reuse ~20 lines --
    same "stay independently runnable" posture that module's own
    ``built_shim_entry`` fixture already states for itself.

    Lands under a plain user-owned scratch directory -- the least privileged
    shape an unseparated install can have, and exactly what `enable` accepts
    directly: it stages its own root:wheel-owned copy internally before
    trusting anything, so this module needs no pre-staged one itself."""
    dmg_path = _built_dmgs()[-1]
    mount_point = Path(tempfile.mkdtemp(prefix="pf-dmg-mount-"))
    subprocess.run(
        ["hdiutil", "attach", str(dmg_path), "-nobrowse", "-readonly", "-mountpoint", str(mount_point)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    try:
        pkg_src = mount_point / "PrivacyFence.pkg"
        assert pkg_src.is_file(), f"PrivacyFence.pkg missing from {dmg_path} (mounted at {mount_point})"
        expanded = dst_dir / "pkg-expanded"
        subprocess.run(
            ["pkgutil", "--expand-full", str(pkg_src), str(expanded)],
            check=True, capture_output=True, text=True, timeout=300,
        )
    finally:
        subprocess.run(["hdiutil", "detach", str(mount_point), "-force"], capture_output=True, text=True, timeout=30)
        shutil.rmtree(mount_point, ignore_errors=True)

    app_bundles = list(expanded.glob("**/Payload/PrivacyFenceApp.app"))
    assert app_bundles, f"no PrivacyFenceApp.app in any component payload of {pkg_src.name}"
    app_dst = dst_dir / "PrivacyFenceApp.app"
    shutil.copytree(app_bundles[0], app_dst, symlinks=True)
    shutil.rmtree(expanded, ignore_errors=True)
    return app_dst


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
    pytest.mark.skipif(shutil.which("pkgutil") is None, reason="pkgutil not on PATH -- needed to unpack the DMG's .pkg"),
    # A real DMG mount, a `pkgutil --expand-full` over the whole PyInstaller payload, a real
    # `enable` (system account creation, data-layout provisioning, two real launchd bootstraps)
    # and polling two real processes to actually come up -- same heavy-setup timeout reasoning as
    # every other packaged/system test in this repo.
    pytest.mark.timeout(240),
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


def _wait_for_path_as_root(
    path: Path, *, timeout: float, what: str, context: Callable[[], str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _sudo_path_exists(path):
            return
        time.sleep(0.1)
    detail = f"\n{context()}" if context is not None else ""
    raise AssertionError(f"{what} ({path}) never appeared within {timeout}s{detail}")


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


def _separated_job_report(domain: str) -> str:
    """Why a job launchd says is running is not serving.

    The same report ``test_macos_pkg_install.py`` has for the same failure,
    a daemon that crash-loops as the wrong account: the socket wait times out
    and everything before it still passes.
    ``_wait_for_running()`` proves only that *a* pid exists under ``domain``,
    and under a ``KeepAlive`` LaunchDaemon that is also exactly what a crash
    loop looks like -- launchd relaunches, the next poll finds the
    replacement, and nothing it reports ever changes. So sample the pid a few
    times over a couple of seconds: a pid that keeps moving is a daemon dying
    and being restarted, which is a different bug from one that came up and is
    merely slow to open its socket, and the two are indistinguishable from the
    timeout alone.

    Alongside it, the three things that say *why*: launchd's own record for
    the job (its last exit status included), the account the live pid is
    actually running as (Failure A and Failure B are separate findings, and
    this is what keeps them told apart in one report), and whatever the daemon
    managed to write under the separated root before it went. All of it needs
    root -- that tree grants ``_privacyfence`` and nothing else, which is why
    none of it shows up in an ordinary capture."""
    pids = []
    for _ in range(5):
        printed = _launchctl_print(domain)
        match = re.search(r"^\s*pid\s*=\s*(\d+)", printed or "", re.MULTILINE)
        pids.append(match.group(1) if match else "none")
        time.sleep(0.5)
    live = [pid for pid in pids if pid != "none"]
    verdict = (
        "pid is stable -- the job is up and not opening its socket"
        if len(set(pids)) == 1 and live
        else "pid CHANGES -- launchd is relaunching a job that keeps exiting (KeepAlive crash loop)"
    )
    sections = [f"---- {domain} pid samples ----\n{' '.join(pids)}\n{verdict}"]
    if live:
        sections.append(f"---- ps -o user=,comm= -p {live[-1]} ----\n{_process_owner(live[-1])} {_process_command(live[-1])}")
    sections.append(f"---- launchctl print {domain} ----\n{_launchctl_print(domain) or '(not loaded)'}")

    listing = _sudo_run("find", str(MACOS_SYSTEM_ROOT), check=False)
    sections.append(f"---- {MACOS_SYSTEM_ROOT} ----\n{listing.stdout}{listing.stderr}")
    for line in listing.stdout.splitlines():
        if line.endswith(".log"):
            tail = _sudo_run("tail", "-n", "80", line, check=False)
            sections.append(f"---- {line} (tail) ----\n{tail.stdout}{tail.stderr}")
    return "\n".join(sections)


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


def _purge_installed_state() -> None:
    """``uninstall --purge`` (ADR 0042): the LaunchDaemon, the companion
    LaunchAgent, the staged image, the data under ``MACOS_SYSTEM_ROOT`` and
    the ``_privacyfence`` account and group. Unconditional: it is idempotent,
    and a run interrupted before ``enable`` wrote its marker can still have
    left an account or a staged image behind."""
    _sudo_run(str(PRIVILEGE_SEPARATION_SCRIPT), "uninstall", "--purge", check=False, timeout=90)


@pytest.fixture
def _clean_separation_state(request):
    """Every test in this module drives ``enable``/``uninstall`` against real
    machine-wide state (a system account, a LaunchDaemon, a LaunchAgent) --
    global state a ``tmp_path`` cannot isolate, the same reason
    ``test_deb_packaged_lifecycle.py``'s own ``_clean_package_state``
    guarantees a clean slate on both sides. Guaranteed at the start too, in
    case a previous, interrupted run never reached its own teardown."""
    _purge_installed_state()
    try:
        yield
    finally:
        _capture_separation_diagnostics(request, os.getuid())
        _purge_installed_state()


# --------------------------------------------------------------------------- #
# The test
# --------------------------------------------------------------------------- #

TRUSTED_IMAGE_DIR = "/Library/PrivacyFence/image"  # macos_privilege_separation.sh's own TRUSTED_IMAGE_DIR


def test_macos_privilege_separation_wires_daemon_and_companion_autostart(_clean_separation_state):
    app_dir = Path(tempfile.mkdtemp(prefix="pf-graphical-session-"))
    try:
        # No pre-staging: `enable` itself copies whatever --app points at into its own
        # root:wheel-owned TRUSTED_IMAGE_DIR before trusting it -- handing
        # it a plain user-owned, /tmp-extracted copy directly is exactly the least-privileged
        # shape this is supposed to accept, not a workaround for it.
        app_path = _copy_app_from_dmg(app_dir)
        user = _current_user()
        uid = os.getuid()

        enable = _sudo_run(str(PRIVILEGE_SEPARATION_SCRIPT), "enable", "--app", str(app_path), "--user", user, timeout=60)
        assert _sudo_path_exists(MARKER_PATH), f"{MARKER_PATH} missing after enable:\n{enable.stdout}{enable.stderr}"
        # `enable` repairs a daemon launchd started as the wrong account rather
        # than leaving it (see start_daemon_as_service_account() in the
        # script). A repaired start is
        # a pass, so nothing below can assert on it; but how often launchd
        # needs the repair is the only measurement anyone has of how real that
        # defect is, and a silent pass throws it away. Same posture as
        # test_macos_pkg_install.py's own app-bundle timing warning.
        if f"not {MACOS_SERVICE_ACCOUNT_NAME}" in enable.stderr:
            warnings.warn(
                f"launchd started {DAEMON_LABEL} as the wrong account and `enable` had to restart it "
                f"(it came up as root):\n{enable.stderr}",
                stacklevel=1,
            )

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
        # Running from the staged copy, not the original --app path this test handed `enable` --
        # the trusted-image staging itself, not just "some binary called PrivacyFenceApp is running".
        daemon_printed = _launchctl_print(f"system/{DAEMON_LABEL}") or ""
        assert TRUSTED_IMAGE_DIR in daemon_printed, (
            f"{DAEMON_LABEL} (pid {daemon_pid}) is not running from {TRUSTED_IMAGE_DIR}:\n{daemon_printed}"
        )

        # Functional proof, not just "launchd thinks it's active": the
        # daemon actually reached the point of writing its own control
        # socket under handoff/ and minting the owner's mcp_token under
        # authority/ (ADR 0008 D3).
        _wait_for_path_as_root(
            socket_path_under(HANDOFF_DIR), timeout=20, what="the separated daemon's control channel socket",
            context=lambda: _separated_job_report(f"system/{DAEMON_LABEL}"),
        )
        _wait_for_path_as_root(
            AUTHORITY_DIR / MCP_TOKEN_FILE_NAME, timeout=20, what="the separated daemon's mcp_token",
            context=lambda: _separated_job_report(f"system/{DAEMON_LABEL}"),
        )

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
        companion_printed = _launchctl_print(companion_domain) or ""
        assert TRUSTED_IMAGE_DIR in companion_printed, (
            f"{COMPANION_LABEL} (pid {companion_pid}) is not running from {TRUSTED_IMAGE_DIR}:\n{companion_printed}"
        )

        _wait_for_path_as_root(
            companion_socket_path_under(HANDOFF_DIR), timeout=20, what="the companion's own control channel socket",
            context=lambda: _separated_job_report(companion_domain),
        )

        # Outlive a few status-poll ticks: a companion that dies after
        # starting is respawned by KeepAlive under a new pid, and the checks
        # above cannot tell that crash loop from a healthy companion.
        time.sleep(3 * _STATUS_POLL_SECONDS)
        still = _wait_for_running(companion_domain, timeout=5)
        assert still == companion_pid, (
            f"{COMPANION_LABEL} died and was respawned (pid {companion_pid} -> {still}) within "
            f"{3 * _STATUS_POLL_SECONDS:.0f}s:\n{_separated_job_report(companion_domain)}"
        )
    finally:
        # TRUSTED_IMAGE_DIR itself is root-owned, but its parent (/Library/PrivacyFence) and
        # everything under it is torn down by `uninstall` -- see _purge_installed_state(), which
        # _clean_separation_state's own teardown already calls unconditionally.
        shutil.rmtree(app_dir, ignore_errors=True)
