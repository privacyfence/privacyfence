"""Real install-time verification for PrivacyFence.pkg (scripts/build_pkg.sh).

``test_macos_graphical_session_autostart.py`` already proves the manual path --
``sudo scripts/macos_privilege_separation.sh enable`` against a DMG-extracted
``.app`` -- actually wires up the LaunchDaemon/LaunchAgent inversion. What that
module does not prove is the whole point of the ``.pkg``: that a real package
*install*, with no separate ``enable`` step and no admin-password runtime
prompt from the daemon (``privilege_separation.maybe_auto_enable_macos()``),
already ends up privilege-separated the moment ``installer`` returns --
because ``installer/macos/pkg/postinstall`` ran ``enable --auto`` itself,
as root, while the package's own postinstall script was already running.

This module drives that for real:

1. **Install**: ``sudo installer -pkg dist/PrivacyFence-<version>.pkg -target /``
   -- a real package install to this runner's actual ``/Applications``, not a
   copy into a scratch directory. Unlike
   ``test_macos_graphical_session_autostart.py``'s own scratch-copy path, this
   needs no ``_stage_as_root`` dance: ``pkgbuild``'s default ownership already
   lays the installed ``.app`` down root:wheel, which is what makes a
   pkg-installed app pass ``require_trusted_image()`` (B1) without the
   codesign-verify substitute proof the runtime prompt needs for a copy the
   user put there themselves (see ``scripts/build_pkg.sh``'s own header
   comment).
2. **No separate ``enable`` call**: this module makes none. If privilege
   separation is on, it is on because the postinstall script already did it.
3. **Same functional assertions** as the graphical-session module: the daemon
   actually running as ``_privacyfence`` from the packaged binary, the
   companion actually running as the CI account from *its* packaged binary,
   and both control-channel sockets + ``mcp_token`` actually present under
   the separated ``handoff/`` directory -- not just "launchd thinks it's
   loaded".

Real GUI installer double-clicks and the admin-password dialog a human would
actually see are out of scope here, same as everywhere else in this repo's
packaged-artifact coverage (``docs/testing-policy.md``'s "what deliberately
remains manual"): ``sudo installer -pkg ... -target /`` is the same
command-line substitution ``test_deb_packaged_lifecycle.py``'s own
``_can_install_packages()`` and ``test_macos_graphical_session_autostart.py``'s
own passwordless-sudo probe already make for the same reason -- nothing in CI
can answer a real password prompt, whether it's ``osascript``'s or
Installer.app's own.

Skipped entirely unless running on real macOS with a just-built
``dist/PrivacyFence-*.pkg`` on disk (``scripts/build_pkg.sh``, itself run
by ``scripts/build_dmg.sh`` -- see ``.github/workflows/macos-graphical-
session.yml``) and passwordless sudo, same posture as the graphical-session
module this one complements.
"""
from __future__ import annotations

import getpass
import os
import platform
import re
import shutil
import subprocess
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
from privacyfence.web.control_channel import companion_socket_path_under, socket_path_under
from tests.diagnostics import failure_dir, suite_name_for, write_environment_info

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "dist"

DAEMON_LABEL = "com.privacyfence.daemon"
COMPANION_LABEL = "com.privacyfence.companion"
PKG_ID = "com.privacyfence.installer"  # scripts/build_pkg.sh's own PKG_ID
INSTALLED_APP_PATH = Path("/Applications/PrivacyFenceApp.app")
MCP_TOKEN_FILE_NAME = "mcp_token"  # web/mcp_auth.py's MCP_TOKEN_FILE_NAME

MARKER_PATH = MACOS_SYSTEM_ROOT / MARKER_FILE_NAME
HANDOFF_DIR = MACOS_SYSTEM_ROOT / HANDOFF_DIR_NAME
# ADR 0008 D3: the owner's own mcp_token lives under the *authority* root,
# not handoff/ -- handoff/ is unaffected (still the control channel socket,
# companion socket, and web_base_url).
AUTHORITY_DIR = MACOS_SYSTEM_ROOT / "authority"


def _built_pkgs() -> list[Path]:
    return sorted(DIST_DIR.glob("PrivacyFence-*.pkg")) if DIST_DIR.is_dir() else []


def _can_sudo() -> bool:
    """Same passwordless-sudo probe ``test_macos_graphical_session_autostart.py``'s
    own ``_can_sudo()`` makes -- installing a system-domain package and
    enabling privilege separation both need root, and this fails fast rather
    than hanging on a password prompt nothing in CI will ever answer."""
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
        not _built_pkgs(),
        reason=(
            "no dist/PrivacyFence-*.pkg built yet -- run scripts/build_dmg.sh first (it builds "
            "the .pkg too), same as test_macos_graphical_session_autostart.py's "
            "own identical skip for the DMG"
        ),
    ),
    pytest.mark.skipif(
        not _can_sudo(), reason="installing a system-domain package needs root -- run as root or with passwordless sudo",
    ),
    pytest.mark.skipif(shutil.which("launchctl") is None, reason="launchctl not on PATH"),
    pytest.mark.skipif(shutil.which("installer") is None, reason="installer(8) not on PATH"),
    # A real system package install (pkgbuild payload extraction + a
    # postinstall script run that itself does everything `enable` does), then
    # polling two real launchd jobs to actually come up -- same heavy-setup
    # timeout as the graphical-session module this one complements.
    pytest.mark.timeout(180),
]


def _sudo_run(*args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    result = subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)
    if check:
        assert result.returncode == 0, f"sudo {' '.join(args)} failed:\n{result.stdout}{result.stderr}"
    return result


def _sudo_path_exists(path: Path) -> bool:
    """Same reasoning as the graphical-session module's own helper of the
    same name: these paths sit under a 0711/0700/2770 tree owned by
    ``_privacyfence``, unreadable by this test's own non-root uid."""
    return subprocess.run(["sudo", "-n", "test", "-e", str(path)], capture_output=True, timeout=10).returncode == 0


def _separated_daemon_report(domain: str) -> str:
    """Why a separated daemon that launchd says is running is not serving.

    ``_wait_for_running()`` proves only that *a* pid exists under ``domain``,
    and under a ``KeepAlive`` LaunchDaemon that is also exactly what a crash
    loop looks like -- launchd relaunches, the poll finds the replacement, and
    nothing it reports ever changes. So this samples the pid a few times over
    a couple of seconds: a pid that keeps moving is a daemon dying and being
    restarted, which is a different bug from one that came up and is merely
    slow to open its socket, and the two are indistinguishable from the
    timeout alone.

    Alongside it, the two things that say *why*: launchd's own record for the
    job (its last exit status included) and whatever the daemon managed to
    write under the separated root before it went. Both need root -- the
    separated tree grants ``_privacyfence`` and nothing else."""
    pids = []
    for _ in range(5):
        printed = _launchctl_print(domain)
        match = re.search(r"^\s*pid\s*=\s*(\d+)", printed or "", re.MULTILINE)
        pids.append(match.group(1) if match else "none")
        time.sleep(0.5)
    verdict = (
        "pid is stable -- the daemon is up and not opening its socket"
        if len(set(pids)) == 1 and pids[0] != "none"
        else "pid CHANGES -- launchd is relaunching a daemon that keeps exiting (KeepAlive crash loop)"
    )
    sections = [f"---- {domain} pid samples ----\n{' '.join(pids)}\n{verdict}"]
    sections.append(f"---- launchctl print {domain} ----\n{_launchctl_print(domain) or '(not loaded)'}")

    listing = _sudo_run("find", str(MACOS_SYSTEM_ROOT), check=False)
    sections.append(f"---- {MACOS_SYSTEM_ROOT} ----\n{listing.stdout}{listing.stderr}")
    for line in listing.stdout.splitlines():
        if line.endswith(".log"):
            tail = _sudo_run("tail", "-n", "80", line, check=False)
            sections.append(f"---- {line} (tail) ----\n{tail.stdout}{tail.stderr}")
    return "\n".join(sections)


def _wait_for_path_as_root(path: Path, *, timeout: float, what: str, context: Callable[[], str] | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _sudo_path_exists(path):
            return
        time.sleep(0.1)
    detail = f"\n{context()}" if context is not None else ""
    raise AssertionError(f"{what} ({path}) never appeared within {timeout}s{detail}")


def _missing_payload_report(pkg_path: Path) -> str:
    """What ``installer`` actually did, for the one failure this module keeps
    hitting and cannot explain: "The install was successful" with nothing at
    ``INSTALLED_APP_PATH`` (#562).

    The receipt says whether installd thinks it installed this package at all;
    ``--files`` says what the receipt claims it laid down and therefore whether
    the payload was empty at *build* time rather than dropped at install time;
    ``/Applications`` says whether the bundle landed under a different name;
    and ``/var/log/install.log`` is installd's own account of the run, which is
    the only source here that can distinguish a package that installed nothing
    from a payload that was removed again immediately afterwards.

    Collected into the assertion message rather than only into this job's
    diagnostics artifact: the artifact is not reachable from every place this
    run gets read, and a failure that reproduces roughly half the time is one
    a reader needs to understand from the log they already have open."""
    sections = [f"pkg under test: {pkg_path}"]
    applications = subprocess.run(
        ["ls", "-la", "/Applications"], capture_output=True, text=True, timeout=30,
    )
    sections.append(f"---- /Applications ----\n{applications.stdout}{applications.stderr}")
    info = _sudo_run("pkgutil", "--pkg-info", PKG_ID, check=False)
    sections.append(f"---- pkgutil --pkg-info {PKG_ID} ----\n{info.stdout}{info.stderr}")
    files = _sudo_run("pkgutil", "--files", PKG_ID, check=False)
    payload = files.stdout.splitlines()
    sections.append(
        f"---- pkgutil --files {PKG_ID} ({len(payload)} entries, first 20) ----\n"
        + "\n".join(payload[:20]) + files.stderr
    )
    install_log = subprocess.run(
        ["tail", "-n", "200", "/var/log/install.log"], capture_output=True, text=True, timeout=30,
    )
    sections.append(f"---- /var/log/install.log (tail) ----\n{install_log.stdout}{install_log.stderr}")
    return "\n".join(sections)


def _wait_for_app_bundle(path: Path, *, timeout: float) -> float | None:
    """Poll for the installed app bundle instead of checking once.

    #562: `installer(8)` was observed reporting "The install was successful"
    with the payload not yet visible at `path` roughly half the time, on
    otherwise-identical runs. Polling here turns that race -- if that's what
    it is -- into a bounded wait instead of a flaky failure, while still
    failing for real if the bundle never shows up at all: this does not
    swallow the possibility that the installer genuinely drops the payload,
    it just stops a few hundred milliseconds of settling time from looking
    like that.

    Returns the number of seconds actually waited on success, or ``None`` if
    ``path`` never appeared within ``timeout``.
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    while True:
        if path.is_dir():
            return time.monotonic() - start
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def _launchctl_print(domain: str) -> str | None:
    result = _sudo_run("launchctl", "print", domain, check=False)
    return result.stdout if result.returncode == 0 else None


def _wait_for_running(domain: str, *, timeout: float) -> str:
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


def _capture_install_diagnostics(request, uid: int) -> None:
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call is None or not rep_call.failed:
        return
    dest = failure_dir(request.node.nodeid, suite=suite_name_for(__file__))
    dest.mkdir(parents=True, exist_ok=True)
    write_environment_info(dest / "environment.txt")

    receipt = _sudo_run("pkgutil", "--pkg-info", PKG_ID, check=False)
    (dest / "pkgutil-pkg-info.txt").write_text(receipt.stdout + receipt.stderr, encoding="utf-8")

    for domain, name in ((f"system/{DAEMON_LABEL}", "daemon"), (f"gui/{uid}/{COMPANION_LABEL}", "companion")):
        printed = _launchctl_print(domain)
        (dest / f"launchctl-print-{name}.txt").write_text(printed or "(not loaded)", encoding="utf-8")

    listing = _sudo_run("find", str(MACOS_SYSTEM_ROOT), check=False)
    (dest / "system-root-manifest.txt").write_text(listing.stdout + listing.stderr, encoding="utf-8")

    install_log = subprocess.run(
        ["tail", "-n", "500", "/var/log/install.log"], capture_output=True, text=True, timeout=10,
    )
    (dest / "install.log.tail").write_text(install_log.stdout + install_log.stderr, encoding="utf-8")


def _uninstall_pkg() -> None:
    """The only "uninstall" a macOS ``.pkg`` has: remove the app it placed
    (there is no generated uninstaller) and forget the receipt (``pkgutil
    --forget``) so a re-run of this module, or of ``test_macos_graphical_
    session_autostart.py`` sharing the same runner, starts from a state
    ``pkgutil --pkg-info`` reports as never installed. Mirrors ``test_macos_
    packaged_smoke.py``'s own step 6 "drag to Trash" gesture, plus the
    receipt cleanup a real drag-install never has to do."""
    if _sudo_path_exists(MARKER_PATH) and INSTALLED_APP_PATH.is_dir():
        _sudo_run(
            str(INSTALLED_APP_PATH / "Contents/Resources/scripts/macos_privilege_separation.sh"),
            "disable", "--user", _current_user(), check=False, timeout=60,
        )
    _sudo_run("rm", "-rf", str(INSTALLED_APP_PATH), check=False)
    _sudo_run("pkgutil", "--forget", PKG_ID, check=False)


@pytest.fixture
def _clean_pkg_state(request):
    """Guaranteed at the start too, in case a previous, interrupted run never
    reached its own teardown -- same reasoning as the graphical-session
    module's own ``_clean_separation_state`` fixture."""
    _uninstall_pkg()
    try:
        yield
    finally:
        _capture_install_diagnostics(request, os.getuid())
        _uninstall_pkg()


def test_pkg_install_enables_privilege_separation_with_no_manual_step(_clean_pkg_state):
    pkg_path = _built_pkgs()[-1]
    user = _current_user()
    uid = os.getuid()

    # -verbose so `install.stdout`, which every assertion below already
    # quotes, records installd's own per-phase progress instead of the three
    # lines it prints by default -- the difference between "the install was
    # successful" and knowing what it considered installing (#562).
    install = _sudo_run("installer", "-verbose", "-pkg", str(pkg_path), "-target", "/", timeout=120)
    assert not INSTALLED_APP_PATH.is_symlink()
    # #562: don't assert immediately -- see _wait_for_app_bundle's own docstring.
    waited = _wait_for_app_bundle(INSTALLED_APP_PATH, timeout=10)
    if waited is not None and waited > 0.5:
        warnings.warn(
            f"{INSTALLED_APP_PATH} took {waited:.2f}s to become visible after `installer` "
            f"reported success (#562) -- installer/installd payload-visibility race, not a "
            f"postinstall failure",
            stacklevel=1,
        )
    assert waited is not None, (
        f"installer did not place {INSTALLED_APP_PATH} within 10s of returning:\n"
        f"{install.stdout}{install.stderr}\n"
        f"{_missing_payload_report(pkg_path)}"
    )

    # The postinstall script ran `enable --auto` itself, synchronously, as
    # part of `installer` above -- no separate `enable` call from this test,
    # which is the entire point of the .pkg over the DMG's own runtime
    # prompt. If this marker is missing, the postinstall script either
    # didn't run or declined to auto-enable (e.g. it could not resolve a
    # console user) -- either way, a real regression this test exists to
    # catch, not something to work around here.
    assert _sudo_path_exists(MARKER_PATH), (
        f"{MARKER_PATH} missing after installing {pkg_path.name} -- postinstall's `enable --auto` "
        f"did not take effect:\n{install.stdout}{install.stderr}"
    )

    # ── The daemon: a LaunchDaemon in the system/ domain, running as the
    # dedicated service account, from the packaged binary `installer` itself
    # just placed under /Applications. ──
    daemon_pid = _wait_for_running(f"system/{DAEMON_LABEL}", timeout=30)
    daemon_owner = _process_owner(daemon_pid)
    assert daemon_owner == MACOS_SERVICE_ACCOUNT_NAME, (
        f"{DAEMON_LABEL} (pid {daemon_pid}) should run as {MACOS_SERVICE_ACCOUNT_NAME}, not {daemon_owner!r}"
    )
    daemon_command = _process_command(daemon_pid)
    assert daemon_command.endswith("PrivacyFenceApp"), (
        f"{DAEMON_LABEL} (pid {daemon_pid}) is not running the packaged daemon binary: {daemon_command!r}"
    )

    _wait_for_path_as_root(
        socket_path_under(HANDOFF_DIR), timeout=20, what="the separated daemon's control channel socket",
        context=lambda: _separated_daemon_report(f"system/{DAEMON_LABEL}"),
    )
    _wait_for_path_as_root(
        AUTHORITY_DIR / MCP_TOKEN_FILE_NAME, timeout=20, what="the separated daemon's mcp_token",
        context=lambda: _separated_daemon_report(f"system/{DAEMON_LABEL}"),
    )

    # ── The companion: a LaunchAgent the postinstall script bootstrapped
    # straight into this runner's own already-logged-in GUI session, exactly
    # like a real human's own console session -- see this module's own
    # docstring for why that's what makes it reachable in CI at all. ──
    companion_domain = f"gui/{uid}/{COMPANION_LABEL}"
    companion_pid = _wait_for_running(companion_domain, timeout=30)
    companion_owner = _process_owner(companion_pid)
    assert companion_owner == user, (
        f"{COMPANION_LABEL} (pid {companion_pid}) should run as {user!r} (the console user), "
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
