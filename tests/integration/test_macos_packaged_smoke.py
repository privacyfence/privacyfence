"""Release-workflow smoke test against the actual packaged macOS artifact
(TST-15).

Every other test in this repo runs against source -- an editable install, or
(tests/integration/test_shim_mcp_contract.py) a freshly-built dist/shim.js
run straight from mcpb/shim/. None of that proves the thing end users
actually download -- ``dist/PrivacyFence-<version>.dmg``, produced by
``scripts/build_dmg.sh`` via PyInstaller (``PrivacyFenceApp.spec``) -- is
correctly laid out, actually starts, and actually speaks MCP once bundled.
PyInstaller's own module-discovery pass has silently dropped a package
before (a hidden import, a missing data file) in a way no source-tree test
can catch, since a source-tree test never leaves the interpreter that
already knows how to import everything.

The DMG carries ``PrivacyFence.pkg`` and ``PrivacyFence.mcpb`` and nothing
else -- no ``PrivacyFenceApp.app`` to drag, no ``/Applications`` symlink (see
``scripts/build_dmg.sh``'s own header for why the .pkg moved inside the image
instead of shipping beside it). So the app bundle this module exercises comes
out of the .pkg's payload, which is the only place it exists in a shipped
artifact at all; ``test_dmg_carries_only_the_installer_and_the_extension``
below asserts that layout directly, so a regression there fails as itself
rather than as an unexplained "app missing from the DMG".

This is deliberately narrow -- one round trip, not a real test suite for
the packaged app:

1. **Install**: mount the just-built DMG (``hdiutil attach``), expand the
   ``PrivacyFence.pkg`` on it (``pkgutil --expand-full``) and take
   ``PrivacyFenceApp.app`` out of the payload ``installer(8)`` would have
   written to ``/Applications`` -- minus actually writing to a shared
   runner's ``/Applications``. The real ``sudo installer -pkg`` install,
   postinstall script and all, is ``test_macos_pkg_install.py``'s job in the
   weekly ``macos-graphical-session.yml`` run, deliberately not this job's --
   see that module's own docstring.
2. **Separate and start the daemon**: ``sudo scripts/macos_privilege_
   separation.sh enable --app <this copy> --user <this CI account>`` --
   the real elevated call the .pkg's own postinstall makes, run by hand
   here for the same reason ``test_macos_graphical_session_autostart.py``
   already does it this way (that module's own docstring). ADR 0003
   decision 6 (``privilege_separation.enforce_separation()``) makes a
   packaged daemon refuse to serve at all once unseparated has no meaning
   left to fall back on -- see this module's own "Real-daemon helpers"
   section below for what running against the real, separated
   ``system/com.privacyfence.daemon`` LaunchDaemon means for the rest of
   this module, in place of the directly-Popen'd, deliberately-unseparated
   process this used to start against a scratch ``$HOME``. Mints a
   bootstrap link the same way a human with root but no daemon-log line
   handy would (through the #428 Phase 2 control channel -- a real Unix
   domain socket against the daemon's own, now root-owned, data
   directory), as root: SEC-10's ``SecretRedactingFormatter`` redacts a
   ``bootstrap=<value>`` substring from every log line on principle, and
   separately, a currently-running process never picks up the ``${SERVICE_
   GROUP}`` membership ``enable`` just granted this account -- only a
   fresh login does (same reasoning ``test_macos_graphical_session_
   autostart.py``'s own sudo-everything posture already documents).
3. **Connect via the MCP shim**: build and spawn the real
   ``mcpb/shim/dist/shim.js`` (same artifact Claude Desktop would run) over
   real stdio, exactly like test_shim_mcp_contract.py -- via
   ``sudo -u <this account> -g ${SERVICE_GROUP}``, the same "simulate the
   fresh login this group membership is actually waiting on" substitution
   step 2 needs for its own reads, since the shim reads ``mcp_url``/
   ``mcp_token`` straight off the group-shared ``handoff/`` directory
   itself (``mcpb/shim/src/protocol.ts``'s own ``privilegeSeparationRoot()``).
4. **Open the approval UI**: a real headless-Chromium page follows the
   bootstrap link (SEC-06), landing signed in on ``/approvals`` -- an
   ordinary HTTP client, so none of the group-membership plumbing above
   applies to it.
5. **One synthetic Allow/Deny round trip**: call
   ``privacyfence_propose_auto_accept_rule_change`` (the one meta-tool that
   always opens a confirmation popup, so this needs no connector OAuth setup
   at all) over MCP, click "Confirm" on the real served card from the real
   browser, and assert the MCP call the whole time was blocked on returns
   the confirmed result once that happens.
6. **State lives outside the package, twice over**: delete the *original*
   scratch copy handed to ``enable --app`` (``installed_app``) and confirm
   the rule change from step 5 is still reachable -- proving the daemon
   runs from its own root-owned, ``enable``-staged copy
   (``/Library/PrivacyFence/image``), wholly independent of the path it was
   pointed at, not just independent of ``$HOME`` the way an unseparated
   install's daemon would be. Then run the real uninstall gesture --
   ``disable`` -- and confirm the same state is now where a plain drag-to-
   Trash removal would actually find it: back under ``$HOME/.privacyfence``,
   the same "user state survives package removal" property
   ``test_deb_packaged_lifecycle.py``'s own ``dpkg -r``/``-P`` and
   ``test_windows_packaged_smoke.py``'s silent uninstall assert for their
   own platforms' removal gesture -- ``disable`` is macOS's.
7. **Signature/notarization**: when the DMG was built with ``--sign``/
   ``NOTARIZE_PROFILE`` (as ``build.yml``'s release job always does; a local
   unsigned dev build is legitimate and skips this instead of failing it),
   verify the installed bundle's code-signature chain (``codesign
   --verify --deep --strict``), that it's actually signed by a real
   Developer ID identity rather than ad-hoc, that Gatekeeper's own policy
   engine would accept it (``spctl --assess --type execute``), and that the
   DMG itself carries a stapled notarization ticket. This proves the
   artifact's Gatekeeper story automatically; the interactive first-launch
   "are you sure you want to open this" dialog a human actually sees stays
   manual, per this plan's own governing rule. Runs against its own private
   copy of the bundle (``signed_app_copy``), not the one step 6 deletes --
   see that fixture's own docstring for why.
8. **Upgrade in place** (deliberately not built in the same PR as steps 1-7): install
   version N, apply real state through the daemon's own MCP surface,
   replace the bundle with a synthetically-relabeled version N+1 (``enable
   --app`` run a second time against the new copy -- idempotent, and what
   re-stages ``TRUSTED_IMAGE_DIR`` and restarts the LaunchDaemon against
   it, per that script's own ``cmd_enable`` comment), and confirm the state
   survived and the new bundle still starts and serves. Its own MCP round
   trip is the lighter direct-``/mcp``-plus-HTTP-decide substitution
   ``test_deb_packaged_lifecycle.py``/``test_windows_packaged_smoke.py``
   already use for their own scenarios, not steps 3-5's real shim/browser --
   the shim artifact itself is already proven by steps 1-5; this step's own
   job is proving state survival across a bundle swap.

Skipped entirely unless running on real macOS with a just-built DMG on disk
(this only makes sense as a step in ``.github/workflows/build.yml``'s
``build`` job, right after ``scripts/build_dmg.sh`` -- see that workflow's
"Run packaged smoke test" step), Node/the ``mcp``/``playwright`` test
extras available, and passwordless sudo (steps 2/3/6/8 above all need real
root) -- never runs as part of the ordinary ``pytest`` invocation in
tests.yml's ubuntu-latest job.
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import functools
import getpass
import platform
import plistlib
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import httpx2
import pytest

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM_DIR = REPO_ROOT / "mcpb" / "shim"
SHIM_ENTRY = SHIM_DIR / "dist" / "shim.js"
DIST_DIR = REPO_ROOT / "dist"
PRIVILEGE_SEPARATION_SCRIPT = REPO_ROOT / "scripts" / "macos_privilege_separation.sh"
MCP_TOKEN_FILE_NAME = "mcp_token"  # web/mcp_auth.py's MCP_TOKEN_FILE_NAME
DAEMON_LABEL = "com.privacyfence.daemon"
SERVICE_GROUP = "privacyfence"

# ADR 0003 decision 6 made a packaged daemon refuse to serve at all unless
# genuinely separated -- see this module's own "Real-daemon helpers" section
# for what that means here. Spelled out independently rather than imported
# from privilege_separation.py, same "don't let the test and the app drift
# together silently" reasoning test_deb_packaged_lifecycle.py's own
# constants give (this module has never imported the `privacyfence` package
# at all, for the same reason -- it tests the frozen binary from outside).
MACOS_SYSTEM_ROOT = Path("/Library/Application Support/PrivacyFence")
AUTHORITY_DIR = MACOS_SYSTEM_ROOT / "authority"
HANDOFF_DIR = MACOS_SYSTEM_ROOT / "handoff"
# Under HANDOFF_DIR, not AUTHORITY_DIR: paths.control_socket_dir() (which
# web/control_channel.py's posix_socket_path() binds to) relocates the
# socket to the handoff directory once privilege separation is enabled,
# because a separated AUTHORITY_DIR is 0700 under the daemon's own service
# account and the companion -- running as the human, not root -- has to
# still be able to connect (see control_socket_dir()'s own docstring).
# AUTHORITY_DIR/control.sock is only where it binds on an *unseparated*
# install, which nothing running against MACOS_SYSTEM_ROOT here ever is.
CONTROL_SOCKET = HANDOFF_DIR / "control.sock"
SEPARATED_SETTINGS_PATH = AUTHORITY_DIR / "config" / "settings.yaml"
SEPARATED_MCP_TOKEN_PATH = HANDOFF_DIR / MCP_TOKEN_FILE_NAME
SEPARATED_WEB_BASE_URL_PATH = HANDOFF_DIR / "web_base_url"
PRIVILEGE_SEPARATION_MARKER = MACOS_SYSTEM_ROOT / "privilege-separation.json"


def _built_dmgs() -> list[Path]:
    return sorted(DIST_DIR.glob("PrivacyFence-*.dmg")) if DIST_DIR.is_dir() else []


def _can_sudo() -> bool:
    """Same passwordless-sudo probe ``test_macos_graphical_session_
    autostart.py``'s own ``_can_sudo()`` makes, for the same reason: real
    privilege separation needs root, and this fails fast rather than
    hanging on a password prompt nothing in CI will ever answer."""
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.skipif(
        platform.system() != "Darwin",
        reason="only meaningful against a real .app/.dmg -- see the plan's TST-15 row",
    ),
    pytest.mark.skipif(
        not _built_dmgs(),
        reason=(
            "no dist/PrivacyFence-*.dmg built yet -- this is the release-workflow smoke test "
            "build.yml's `build` job runs after scripts/build_dmg.sh; run that script locally "
            "first to exercise this test outside CI"
        ),
    ),
    pytest.mark.skipif(shutil.which("node") is None, reason="Node not on PATH -- this test spawns the real shim"),
    pytest.mark.skipif(shutil.which("pkgutil") is None, reason="pkgutil not on PATH -- needed to unpack the DMG's .pkg"),
    pytest.mark.skipif(
        not _can_sudo(),
        reason="enabling real privilege separation needs root -- run as root or with passwordless sudo",
    ),
    # DMG mount/copy + a real PyInstaller cold start + npm install/build (first run per session)
    # + a real `enable` (system account creation, data-layout provisioning, two real launchd
    # bootstraps -- test_macos_graphical_session_autostart.py's own timeout(240) covers `enable`
    # alone) + a real headless-browser round trip is comfortably slower than pure-Python socket
    # tests -- same reasoning as test_shim_mcp_contract.py's own inflated timeout for the
    # npm-install cost, plus this test's own daemon startup and browser work on top.
    pytest.mark.timeout(360),
    pytest.mark.packaged,
]


@contextlib.contextmanager
def _mounted_dmg():
    """Mounts the just-built DMG read-only and yields the mount point, always
    detaching on the way out even on failure."""
    dmg_path = _built_dmgs()[-1]
    mount_point = Path(tempfile.mkdtemp(prefix="pf-dmg-mount-"))
    subprocess.run(
        ["hdiutil", "attach", str(dmg_path), "-nobrowse", "-readonly", "-mountpoint", str(mount_point)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    try:
        yield mount_point
    finally:
        subprocess.run(
            ["hdiutil", "detach", str(mount_point), "-force"], capture_output=True, text=True, timeout=30,
        )
        shutil.rmtree(mount_point, ignore_errors=True)


@functools.lru_cache(maxsize=1)
def _extracted_app() -> Path:
    """The one ``PrivacyFenceApp.app`` every test here installs a copy of,
    taken out of the ``PrivacyFence.pkg`` the DMG carries (the app exists in
    no other form in a shipped artifact -- see this module's own docstring).

    Mounts the DMG and expands the package exactly once per test session --
    ``pkgutil --expand-full`` walks the whole PyInstaller payload, so doing it
    per-copy would cost more than every daemon start in this module put
    together. The extraction directory is this function's own, cleaned up at
    interpreter exit; callers never get it directly, only copies of it."""
    workdir = Path(tempfile.mkdtemp(prefix="pf-dmg-pkg-"))
    atexit.register(shutil.rmtree, workdir, ignore_errors=True)
    dmg_path = _built_dmgs()[-1]
    with _mounted_dmg() as mount_point:
        pkg_src = mount_point / "PrivacyFence.pkg"
        assert pkg_src.is_file(), (
            f"PrivacyFence.pkg missing from {dmg_path} (mounted at {mount_point}, holding "
            f"{sorted(p.name for p in mount_point.iterdir())})"
        )
        expanded = workdir / "expanded"
        expand = subprocess.run(
            ["pkgutil", "--expand-full", str(pkg_src), str(expanded)],
            capture_output=True, text=True, timeout=300,
        )
        assert expand.returncode == 0, f"pkgutil --expand-full {pkg_src} failed:\n{expand.stdout}{expand.stderr}"

    # One component package, installing to /Applications -- so its Payload/ holds the bundle
    # itself. Globbed rather than spelled out, for the same reason test_macos_pkg_smoke.py globs
    # it: the component package's own filename carries the version.
    app_bundles = list(expanded.glob("**/Payload/PrivacyFenceApp.app"))
    assert app_bundles, f"no PrivacyFenceApp.app in any component payload of {pkg_src.name}"
    return app_bundles[0]


def _copy_app_from_dmg(dst_dir: Path) -> Path:
    """Copies ``PrivacyFenceApp.app`` out of the shipped DMG's installer
    package into ``dst_dir`` -- the "install" step, without writing to this
    runner's real ``/Applications``. The caller owns ``dst_dir``'s own
    lifecycle (this only ever writes into it, never removes it).

    A plain function, not a fixture, so more than one test can get its own
    independent copy of the bundle without sharing mutable state through a
    fixture -- see ``signed_app_copy`` and
    ``test_macos_upgrade_preserves_user_state`` below for why that
    independence matters here specifically."""
    app_dst = dst_dir / "PrivacyFenceApp.app"
    shutil.copytree(_extracted_app(), app_dst, symlinks=True)
    return app_dst


def test_dmg_carries_only_the_installer_and_the_extension():
    """The shipped macOS artifact is a carrier for two files: the installer
    that provisions privilege separation at install time (#428 D2) and the
    Claude Desktop extension the .pkg's own conclusion screen tells the user
    to open "next to this installer" -- a sentence that is only true because
    both are on this image.

    The drag-install layout this replaced (``PrivacyFenceApp.app`` plus an
    ``/Applications`` symlink) is asserted *absent*, not merely "not
    required": leaving it in would give the same download two install paths,
    one of which silently skips the installer and gets the deferred
    admin-password prompt instead."""
    with _mounted_dmg() as mount_point:
        # Dot-prefixed entries are the volume's own metadata (.VolumeIcon.icns, .fseventsd,
        # .Trashes...), never anything this repo puts there -- excluded so an HFS+ housekeeping
        # file can't fail a release build over a layout that is in fact correct.
        entries = sorted(p.name for p in mount_point.iterdir() if not p.name.startswith("."))
        assert "PrivacyFence.pkg" in entries, f"no installer on the shipped DMG: {entries}"
        assert "PrivacyFence.mcpb" in entries, (
            f"no Claude Desktop extension on the shipped DMG: {entries} -- the installer's own "
            f"conclusion screen tells the user to open it right there"
        )
        assert "PrivacyFenceApp.app" not in entries, (
            f"the app bundle is back on the DMG: {entries} -- dragging it out is a second install "
            f"path that skips the .pkg's install-time privilege separation entirely"
        )
        assert "Applications" not in entries, (
            f"the /Applications drop link is back: {entries} -- same second-install-path problem"
        )


@pytest.fixture(scope="module")
def installed_app() -> Path:
    """The shared copy of ``PrivacyFenceApp.app`` used by the primary
    round-trip test and (via ``signed_app_copy``'s own reasoning) nothing
    else -- module-scoped because the primary test mutates and then deletes
    it (module docstring §6). The DMG mount and package expansion behind it
    are shared across the whole session regardless, by ``_extracted_app``."""
    install_dir = Path(tempfile.mkdtemp(prefix="pf-dmg-install-"))
    try:
        yield _copy_app_from_dmg(install_dir)
    finally:
        shutil.rmtree(install_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Real-daemon helpers.
#
# Before ADR 0003 decision 6 (4bcafc0/776128f, landed after v4.1.0a10 -- the
# last tag this module's own daemon-lifecycle helpers were green against),
# this module ran the packaged binary directly against a scratch,
# deliberately-unseparated $HOME for a fast round trip -- privilege_
# separation.maybe_auto_enable_macos() was best-effort and non-fatal, so an
# unseparated daemon still served. Decision 6 retired that outright: a
# packaged daemon that finds itself unseparated now refuses to serve at all
# (privilege_separation.enforce_separation()), unconditionally, with no
# override for a test harness or anything else. So this module now runs the
# real elevated `enable` call test_macos_graphical_session_autostart.py
# already established the pattern for, and talks to the real, separated
# system/com.privacyfence.daemon LaunchDaemon that leaves running.
#
# Its files are root-owned (settings.yaml/the control socket under
# authority/, 0700; mcp_token/web_base_url under handoff/, 2770 group
# ${SERVICE_GROUP}) -- and even though this CI account was just added to
# ${SERVICE_GROUP} by `enable`, a process already running when that happens
# never picks it up, only a fresh login does (same reasoning
# test_macos_graphical_session_autostart.py's own sudo-everything posture
# already documents for the identical problem on this same account). So
# every read below goes through `sudo -n`, and the Node shim -- the one
# thing in this module that reads those files as a plain, non-sudo child
# process, because that is what the real Claude Desktop does -- is spawned
# via `sudo -u <this account> -g ${SERVICE_GROUP}` instead: the same "fresh
# login" `enable`'s own printed note says this account is waiting on,
# simulated rather than skipped, since nothing in CI can actually log back
# in.
# --------------------------------------------------------------------------- #

def _sudo_capture(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)


def _sudo_run(*args: str, check: bool = True, timeout: float = 90) -> subprocess.CompletedProcess:
    result = _sudo_capture(*args, timeout=timeout)
    if check:
        assert result.returncode == 0, f"sudo {' '.join(args)} failed:\n{result.stdout}{result.stderr}"
    return result


def _sudo_read_text(path: Path, *, timeout: float = 15) -> str | None:
    """``sudo -n cat`` -- see this section's own module comment. ``None``
    (not an exception) when the file does not exist yet, the same "not
    there" signal a direct ``.exists()`` would give a poll loop, which is
    this helper's main caller."""
    result = _sudo_capture("cat", str(path), timeout=timeout)
    return result.stdout if result.returncode == 0 else None


def _sudo_mint_bootstrap_code(socket_path: Path, *, timeout: float = 5.0) -> str:
    """Speaks the control channel's own one-line ``MINT`` protocol
    (tests/control_channel_client.py's ``mint_bootstrap_code_posix()``,
    which this can't call directly -- it has to run as root) via a small
    stdlib-only inline script instead -- same technique
    test_deb_packaged_lifecycle.py's own identically-named helper uses."""
    script = (
        "import socket,sys\n"
        f"s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        f"s.settimeout({timeout})\n"
        f"s.connect({str(socket_path)!r})\n"
        "s.sendall(b'MINT\\n')\n"
        "sys.stdout.write(s.recv(4096).decode('utf-8'))\n"
    )
    result = _sudo_capture("python3", "-c", script, timeout=timeout + 5)
    assert result.returncode == 0, f"minting a bootstrap code as root failed:\n{result.stdout}{result.stderr}"
    reply = result.stdout
    assert reply.startswith("OK "), f"control channel mint failed: {reply!r}"
    return reply[len("OK "):].strip()


def _launchctl_print(domain: str) -> str | None:
    result = _sudo_capture("launchctl", "print", domain)
    return result.stdout if result.returncode == 0 else None


def _wait_for_running(domain: str, *, timeout: float) -> str:
    """Polls ``launchctl print <domain>`` until it reports a real pid --
    same helper test_macos_graphical_session_autostart.py already uses."""
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


def _enable_separation(app_path: Path, *, user: str, timeout: float = 120.0) -> None:
    _sudo_run(str(PRIVILEGE_SEPARATION_SCRIPT), "enable", "--app", str(app_path), "--user", user, timeout=timeout)


def _disable_if_separated(*, user: str) -> None:
    if _sudo_capture("test", "-e", str(PRIVILEGE_SEPARATION_MARKER)).returncode == 0:
        _sudo_run(str(PRIVILEGE_SEPARATION_SCRIPT), "disable", "--user", user, check=False, timeout=60)


class RunningDaemon:
    """The real, launchd-managed ``system/com.privacyfence.daemon`` -- not
    something this module owns a ``subprocess.Popen`` handle for any more,
    see this section's own module comment."""

    def __init__(self, base_url: str, bootstrap_url: str, mcp_token: str):
        self.base_url = base_url
        self.bootstrap_url = bootstrap_url
        self.mcp_token = mcp_token

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"


def _wait_for_real_daemon(*, timeout: float = 30.0) -> RunningDaemon:
    """Waits for the real ``system/com.privacyfence.daemon`` LaunchDaemon
    ``enable`` just (re)started to actually come up, and returns a
    ``RunningDaemon`` for it -- same technique (and same reasoning) as
    test_deb_packaged_lifecycle.py's own ``_wait_for_real_daemon()``:
    ``web_base_url`` (web/control_channel.py's own
    ``WEB_BASE_URL_FILE_NAME``) is the daemon's own way of telling the
    companion its port, and is cleared on ``WebServer.stop()``, which is
    what lets this tell "not up yet" apart from "still the previous boot's
    value" the second time this module calls it."""
    _wait_for_running(f"system/{DAEMON_LABEL}", timeout=timeout)

    deadline = time.monotonic() + timeout
    base_url = None
    while time.monotonic() < deadline:
        base_url = _sudo_read_text(SEPARATED_WEB_BASE_URL_PATH, timeout=5)
        if base_url and base_url.strip():
            base_url = base_url.strip()
            break
        base_url = None
        time.sleep(0.2)
    assert base_url, (
        f"{SEPARATED_WEB_BASE_URL_PATH} never appeared within {timeout}s -- "
        f"{_launchctl_print(f'system/{DAEMON_LABEL}')}"
    )

    parts = urlsplit(base_url)
    remaining = max(1.0, deadline - time.monotonic())
    deadline2 = time.monotonic() + remaining
    last_exc: OSError | None = None
    while time.monotonic() < deadline2:
        try:
            with socket.create_connection((parts.hostname or "localhost", parts.port), timeout=0.2):
                break
        except OSError as exc:
            last_exc = exc
            time.sleep(0.1)
    else:
        raise TimeoutError(f"{base_url} never became connectable within {remaining}s") from last_exc

    mcp_token = None
    while time.monotonic() < deadline:
        mcp_token = _sudo_read_text(SEPARATED_MCP_TOKEN_PATH, timeout=5)
        if mcp_token and mcp_token.strip():
            mcp_token = mcp_token.strip()
            break
        mcp_token = None
        time.sleep(0.2)
    assert mcp_token, f"{SEPARATED_MCP_TOKEN_PATH} never appeared within {timeout}s"

    code = _sudo_mint_bootstrap_code(CONTROL_SOCKET)
    bootstrap_url = f"{base_url}/approvals?bootstrap={code}"
    return RunningDaemon(base_url, bootstrap_url, mcp_token)


@pytest.fixture
def running_packaged_daemon(installed_app):
    """The primary round-trip test's own daemon: real privilege separation
    against ``installed_app``'s own scratch copy (``enable`` stages its own
    root-owned copy under ``/Library/PrivacyFence/image`` -- see this
    module's own docstring §2/§6 for why handing it a plain, user-owned
    copy is exactly the right, least-privileged input, not a shortcut),
    undone again on the way out regardless of what the test itself already
    did to it (``_disable_if_separated`` is idempotent -- a no-op once
    nothing is separated any more)."""
    user = getpass.getuser()
    _enable_separation(installed_app, user=user)
    try:
        yield _wait_for_real_daemon()
    finally:
        _disable_if_separated(user=user)


@pytest.fixture(scope="module")
def built_shim_entry() -> Path:
    """(Re)builds mcpb/shim/dist/shim.js once per module -- identical
    reasoning to test_shim_mcp_contract.py's own fixture of the same name
    (not imported from there: every contract test in this directory stays
    independently runnable, per that module's own precedent)."""
    if shutil.which("npm") is None:
        pytest.skip("npm not on PATH -- this fixture builds the shim via `npm install`/`npm run build`")
    try:
        subprocess.run(
            ["npm", "install", "--silent"], cwd=SHIM_DIR, check=True, capture_output=True, timeout=180,
        )
        subprocess.run(
            ["npm", "run", "build", "--silent"], cwd=SHIM_DIR, check=True, capture_output=True, timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"could not build mcpb/shim/dist/shim.js: {exc}")
    if not SHIM_ENTRY.exists():
        pytest.skip(f"{SHIM_ENTRY} missing after build")
    return SHIM_ENTRY


def _confirm_pending_rule_change(bootstrap_url: str) -> None:
    """Runs entirely on its own thread (see the test below) -- Playwright's
    sync API must never be invoked from a thread with a running asyncio
    event loop, which is exactly the thread the test's own ``await``s run
    on (see test_browser_smoke.py's ``browser`` fixture docstring for the
    same constraint from the other direction). Opens the real bootstrap
    link, follows the one pending confirmation card to its own page, and
    clicks "Confirm" -- the same real-DOM click
    test_browser_smoke.py's TestApprovalDecisionFlow proves against the
    injected bridge shim (web/routes_approvals.py's ``_bridge_shim``),
    exercised here against the actual packaged app instead of a dev
    WebServer."""
    parts = urlsplit(bootstrap_url)
    base_url = f"{parts.scheme}://{parts.netloc}"
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not available for Playwright ({exc}) -- run `playwright install chromium`")
            return
        try:
            page = browser.new_page()
            page.goto(bootstrap_url)
            page.wait_for_load_state("load")
            assert page.url == f"{base_url}/approvals", f"bootstrap sign-in landed on {page.url!r}"

            row = page.wait_for_selector("[data-approval-id]", timeout=60_000)
            approval_id = row.get_attribute("data-approval-id")
            page.click(f'[data-approval-id="{approval_id}"] a.pf-btn-review')
            page.wait_for_url(f"{base_url}/approvals/{approval_id}")
            # The card's own JS only clears `aria-disabled` (what its click
            # handler actually gates on -- dialog_window_html.py's `_JS`)
            # once DOMContentLoaded fires; Playwright's click-actionability
            # checks the real `disabled` DOM property, not this ARIA
            # attribute, so a click issued before that would silently no-op
            # the same way test_browser_smoke.py's own card-page flow avoids
            # by waiting for "load" first.
            page.wait_for_load_state("load")
            page.locator('[data-pf-action="confirm"]').click()
            page.wait_for_url(f"{base_url}/approvals")
        finally:
            browser.close()


async def test_packaged_app_connects_over_mcp_and_completes_an_approval_round_trip(
    running_packaged_daemon, built_shim_entry, installed_app,
):
    """The one end-to-end assertion this whole module exists for: the real
    ``.mcpb`` shim, talking to the real packaged daemon started from the
    real DMG, round-trips a gated meta-tool call through a real human
    decision made by clicking a real button in a real browser.

    Spawned via ``sudo -u <this account> -g ${SERVICE_GROUP}``, not a plain
    ``node`` child -- see this module's own "Real-daemon helpers" section
    for why a process spawned by this same still-running test session never
    picks up the group membership ``enable`` just granted it. No ``HOME``
    override: the shim's own ``privilegeSeparationRoot()``
    (``mcpb/shim/src/protocol.ts``) already prefers the real, marker-named
    system root over anything ``$HOME``-relative once separation is on,
    which it now always is by the time this fixture yields."""
    user = getpass.getuser()
    params = StdioServerParameters(
        command="sudo", args=["-n", "-u", user, "-g", SERVICE_GROUP, "node", str(built_shim_entry)],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "privacyfence_propose_auto_accept_rule_change" in names
            assert "privacyfence_check_policy" in names

            call_task = asyncio.create_task(
                session.call_tool(
                    "privacyfence_propose_auto_accept_rule_change",
                    {
                        "target": "rule",
                        "operation": "add",
                        "operation_key": "gmail.read_message",
                        "rule_name": "trusted_sender_domain",
                        "value": ["example.com"],
                        "reason": "TST-15 packaged-app smoke test synthetic approval round trip",
                    },
                )
            )
            # Runs on a worker thread so its own (Playwright-internal) event
            # loop never collides with this coroutine's -- see
            # _confirm_pending_rule_change's own docstring.
            await asyncio.to_thread(_confirm_pending_rule_change, running_packaged_daemon.bootstrap_url)
            result = await call_task

    assert result.is_error is not True, getattr(result, "content", result)
    assert result.structured_content is not None
    assert result.structured_content["confirmed"] is True
    assert result.structured_content["changed"] is True
    # P9 of the policy v2 redesign: the confirmed-response description is the v2 rule's own
    # human-readable sentence now, not an echo of the v1 rule_name string.
    assert "Gmail - sender domain example.com: allow read" in result.structured_content["description"]

    # Confirms the round trip actually reached persisted state, not just a
    # confirmed-but-inert in-memory result.
    settings_text = _sudo_read_text(SEPARATED_SETTINGS_PATH)
    assert settings_text and "trusted_sender_domain" in settings_text, settings_text

    # ── State lives outside the package, twice over (module docstring, §6) ──
    # First: delete the *original* scratch copy `enable --app` was pointed
    # at, not the copy the daemon actually runs from any more (`enable`
    # stages its own root-owned copy under /Library/PrivacyFence/image --
    # see this module's own "Real-daemon helpers" section). The daemon
    # keeps running and the state above stays reachable regardless --
    # proving independence from the *original* bundle path, a stronger
    # claim than plain $HOME-independence would be on an unseparated
    # install. This deletes the shared `installed_app` fixture's own copy,
    # not a throwaway one -- deliberately, which is exactly why
    # `test_packaged_app_signature_and_notarization` below no longer
    # depends on `installed_app` still existing afterward (see that test's
    # own `signed_app_copy` fixture).
    shutil.rmtree(installed_app)
    assert not installed_app.exists()
    settings_text = _sudo_read_text(SEPARATED_SETTINGS_PATH)
    assert settings_text and "trusted_sender_domain" in settings_text, (
        "deleting the original --app copy must not affect the daemon's own staged copy"
    )

    # Second: the real uninstall gesture. macOS has no installer/uninstaller
    # pair -- "uninstalling" a separated install is `disable`, which moves
    # state back into $HOME the same deliberate way debian/prerm's own
    # `disable` call does for the .deb (test_deb_packaged_lifecycle.py's own
    # Test 1) -- the "user state survives package removal" property
    # test_windows_packaged_smoke.py's silent uninstall also asserts, for
    # its own platform's removal gesture.
    user = getpass.getuser()
    restored_settings_path = Path.home() / ".privacyfence" / "authority" / "config" / "settings.yaml"
    try:
        _sudo_run(str(PRIVILEGE_SEPARATION_SCRIPT), "disable", "--user", user)
        assert restored_settings_path.exists(), (
            f"`disable` should have restored state to {restored_settings_path}"
        )
        assert "trusted_sender_domain" in restored_settings_path.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(Path.home() / ".privacyfence", ignore_errors=True)


@pytest.fixture
def signed_app_copy() -> Path:
    """A private copy of the extracted bundle, independent of the shared
    module-scoped ``installed_app`` fixture: the primary round-trip test
    above deletes *its* copy of ``installed_app`` partway through its own
    run (module docstring §6) to prove package removal never touches
    ``$HOME`` -- since pytest runs test functions in file-definition order
    by default, a signature test that depended on that same shared fixture
    would silently find nothing there afterward (``codesign`` against a
    missing path exits non-zero the same way an unsigned bundle does,
    which this test's own skip-on-nonzero logic can't tell apart from the
    real "unsigned" case). A dedicated copy sidesteps the ordering
    question entirely rather than depending on it."""
    install_dir = Path(tempfile.mkdtemp(prefix="pf-dmg-signtest-"))
    try:
        yield _copy_app_from_dmg(install_dir)
    finally:
        shutil.rmtree(install_dir, ignore_errors=True)


def test_packaged_app_signature_and_notarization(signed_app_copy):
    """§7 of the module docstring -- distinct from the MCP/approval round
    trip above, and deliberately its own test so a signature failure and an
    approval-protocol failure are never conflated in one report.

    ``scripts/build_dmg.sh``'s own ``--sign``/``NOTARIZE_PROFILE`` are both
    optional (a local dev build with no Developer ID identity is a
    legitimate, common case -- see that script's step 5/8 comments), so this
    skips outright, rather than failing, when the artifact under test wasn't
    built with them. ``build.yml``'s real release job always sets both (see
    its "Build .app and DMG" step), so this asserts the real chain there.
    """
    identify = subprocess.run(
        ["codesign", "-dv", "--verbose=4", str(signed_app_copy)],
        capture_output=True, text=True,
    )
    # codesign writes its `-d` info to stderr; an entirely unsigned bundle
    # exits 1 with "code object is not signed at all".
    if identify.returncode != 0:
        pytest.skip(
            f"PrivacyFenceApp.app is unsigned for this build (SIGN_IDENTITY unset) -- "
            f"run scripts/build_dmg.sh --sign '<identity>' to exercise this test: {identify.stderr}"
        )
    assert "Authority=Developer ID Application" in identify.stderr, (
        f"expected a real Developer ID signature, not an ad-hoc one:\n{identify.stderr}"
    )

    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(signed_app_copy)],
        capture_output=True, text=True,
    )
    assert verify.returncode == 0, f"codesign --verify --deep --strict failed:\n{verify.stderr}"

    # Gatekeeper's own policy engine, asked the same question it asks on a
    # real first launch, without actually invoking the interactive
    # "are you sure you want to open this" dialog a human sees (that part
    # of the story stays manual, per this plan's own governing rule).
    assess = subprocess.run(
        ["spctl", "--assess", "--type", "execute", "--verbose", str(signed_app_copy)],
        capture_output=True, text=True,
    )
    assert assess.returncode == 0, f"Gatekeeper would reject this app (spctl --assess):\n{assess.stdout}{assess.stderr}"

    # Notarization is checked against the DMG itself (what ships), not the
    # extracted bundle -- `xcrun stapler staple` (build_dmg.sh's last step)
    # staples the ticket to the disk image. (The .pkg inside it is stapled
    # separately, by scripts/build_pkg.sh; pkgutil --check-signature over that
    # is test_macos_pkg_smoke.py's own assertion, not this one's.) A signed-but-not-notarized local
    # build (NOTARIZE_PROFILE unset) is also legitimate and must not fail
    # this test -- `spctl`'s own "source=" line is what actually says
    # whether Apple's notarization ticket was found and accepted, without
    # this test needing network access to ask Apple directly the way
    # `xcrun stapler validate` can fall back to doing.
    dmg_path = _built_dmgs()[-1]
    dmg_assess = subprocess.run(
        ["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose", str(dmg_path)],
        capture_output=True, text=True,
    )
    dmg_assess_output = dmg_assess.stdout + dmg_assess.stderr
    if "source=Notarized Developer ID" not in dmg_assess_output:
        pytest.skip(
            f"{dmg_path} is signed but not notarized for this build (NOTARIZE_PROFILE unset): "
            f"{dmg_assess_output}"
        )
    assert dmg_assess.returncode == 0, f"spctl rejected the notarized DMG outright:\n{dmg_assess_output}"


# --------------------------------------------------------------------------- #
# Upgrade in place preserves user state (Phase 6 item 20)
# --------------------------------------------------------------------------- #

def _bump_bundle_version(app_path: Path) -> str:
    """Rewrites ``Contents/Info.plist``'s ``CFBundleShortVersionString`` to a
    value guaranteed different from the original -- the macOS analogue of
    ``test_deb_packaged_lifecycle.py``'s ``_synthetic_next_version_deb`` and
    ``test_windows_packaged_smoke.py``'s ``_synthetic_next_version_
    installer``: same bytes, just relabeled, since what this test needs
    proven (``$HOME`` survives a reinstall) doesn't depend on anything
    actually changing *inside* the bundle. Unlike either of those platforms'
    installers, macOS has no version-ordering (or any other) gate an
    "install" has to satisfy at all -- per the module docstring's own §6,
    "installing" a new version here is just replacing the ``.app`` bundle
    wholesale -- so any different string is enough to prove this is a
    distinct bundle rather than the identical one being re-applied."""
    info_plist_path = app_path / "Contents" / "Info.plist"
    with open(info_plist_path, "rb") as f:
        info = plistlib.load(f)
    new_version = f"{info['CFBundleShortVersionString']}+upgradetest1"
    info["CFBundleShortVersionString"] = new_version
    with open(info_plist_path, "wb") as f:
        plistlib.dump(info, f)

    # Rewriting Info.plist breaks the bundle's code signature: its hash is part
    # of what the main executable's own signature seals, so the copy that came
    # out of the (real, Developer ID signed and notarized) DMG stops validating
    # the moment that file changes. On the arm64 runners this job actually uses,
    # that is not a warning -- the kernel refuses to exec a binary whose
    # signature does not validate at all, so the relabeled bundle died on the
    # spot and the test saw only its web port never opening. Re-signing ad hoc
    # ("-") puts a valid signature back on the modified bundle, which is all
    # the kernel needs to exec it at all, whether that's a direct exec or
    # (this module's own daemon fixtures) launchd loading a LaunchDaemon
    # plist that points at it -- Gatekeeper is deliberately not in the
    # picture here either way, since this bundle is synthetic and its
    # Developer ID provenance is not what this test is about --
    # ``test_packaged_app_signature_and_notarization`` checks that, against
    # the real unmodified bundle.
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(app_path)],
        check=True, capture_output=True, text=True,
    )
    return new_version


async def _propose_trusted_sender_rule(mcp_url: str, mcp_token: str, *, value: list[str]):
    """Applies a real auto-accept rule change over the daemon's own ``/mcp``
    Streamable HTTP endpoint directly -- unlike the primary round-trip test
    above, this doesn't go through the real Node shim or a real browser
    click: that specific substitution (direct MCP client + a raw HTTP POST
    to the decide route, see ``_resolve_pending_card`` below) is the same
    one ``test_deb_packaged_lifecycle.py``/``test_windows_packaged_smoke.py``
    already make for their own scenarios, for the same reason -- the shim
    artifact itself is already proven by the primary round-trip test in
    this module; this test's own job is proving state survival across a
    bundle swap, not re-proving the shim a second time."""
    headers = {"Authorization": f"Bearer {mcp_token}"}
    async with httpx2.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(mcp_url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "privacyfence_propose_auto_accept_rule_change",
                    {
                        "target": "rule",
                        "operation": "add",
                        "operation_key": "gmail.read_message",
                        "rule_name": "trusted_sender_domain",
                        "value": value,
                        "reason": "tests/integration/test_macos_packaged_smoke.py upgrade-in-place scenario",
                    },
                )


async def _resolve_pending_card(base_url: str, bootstrap_url: str) -> None:
    """Bootstraps a real session cookie via the real ``?bootstrap=`` exchange
    (same endpoint ``_wait_for_real_daemon()`` itself already minted the
    code for), polls ``/approvals`` for the one pending card the concurrently-
    running MCP call above just opened, then resolves it via a direct HTTP
    POST to the real decide route -- ``test_deb_packaged_lifecycle.py``'s
    own helper of the same name, adapted to a bootstrap *URL* (this module's
    own shape) rather than a bare token."""
    async with httpx.AsyncClient(base_url=base_url, follow_redirects=True) as web_client:
        exchange = await web_client.get(bootstrap_url)
        assert exchange.status_code == 200, exchange.text
        session_id = web_client.cookies.get("pf_session")
        assert session_id, "bootstrap exchange did not set a pf_session cookie"

        deadline = time.monotonic() + 20.0
        approval_id = None
        while time.monotonic() < deadline:
            page = await web_client.get("/approvals")
            assert page.status_code == 200, page.text
            # Matches both the plain and the binder's "unbatchable" modifier
            # class (approval_list_html.py's _row_html: a confirm-kind card,
            # like the rule-confirmation one this scenario drives, is never
            # batchable) -- see approval_list_html.py's own row_class comment.
            match = re.search(
                r'<div class="pf-approval-row(?: pf-approval-row-unbatchable)?" data-approval-id="([0-9a-f]{16,})"',
                page.text,
            )
            if match:
                approval_id = match.group(1)
                break
            await asyncio.sleep(0.1)
        assert approval_id, "no pending approval card appeared on /approvals"

        decide_resp = await web_client.post(
            f"/api/approvals/{approval_id}/decide", json={"result": "confirm", "csrf": session_id},
        )
        assert decide_resp.status_code == 200, decide_resp.text
        assert decide_resp.json() == {"status": "ok"}


@pytest.mark.timeout(420)   # builds a second bundle copy *and* boots the daemon twice via two
                             # real, elevated `enable` calls -- the module's own timeout=360
                             # already assumes one `enable` plus a real browser round trip, so
                             # this needs the same headroom again for the second `enable`.
async def test_macos_upgrade_preserves_user_state():
    """Phase 6 item 20 -- deliberately not built in the same PR as this
    module's own 6.1 gap-closing work: install version N, use it to create
    real on-disk state (an applied auto-accept rule, via the real MCP round
    trip -- not a hand-written settings.yaml), replace it with a
    synthetically-relabeled version N+1, and confirm the state survived and
    the new bundle still starts and serves. Own, independent bundle copies
    throughout (``_copy_app_from_dmg``) -- see ``signed_app_copy``'s own
    docstring for why this module doesn't share mutable fixture state like
    that across tests.

    Both "installs" below are the same real, elevated ``enable --app``
    ``running_packaged_daemon`` uses -- ``cmd_enable``'s own ``launchctl
    bootout`` runs unconditionally, before it restages ``TRUSTED_IMAGE_DIR``
    from whichever bundle it was just pointed at, so calling it a second
    time against ``app_n1`` is what "installs" the new version in place,
    with no separate stop step needed first.
    """
    install_dir = Path(tempfile.mkdtemp(prefix="pf-dmg-upgrade-"))
    user = getpass.getuser()
    try:
        # ── Install version N; create real on-disk state through the real
        # separated daemon and a real MCP/approval round trip ─────────────
        app_n = _copy_app_from_dmg(install_dir)
        _enable_separation(app_n, user=user)
        daemon = _wait_for_real_daemon()
        propose_task = asyncio.create_task(
            _propose_trusted_sender_rule(daemon.mcp_url, daemon.mcp_token, value=["preupgrade.example.com"])
        )
        await _resolve_pending_card(daemon.base_url, daemon.bootstrap_url)
        result = await propose_task
        assert result.is_error is not True, getattr(result, "content", result)
        assert result.structured_content["changed"] is True

        settings_text = _sudo_read_text(SEPARATED_SETTINGS_PATH)
        assert settings_text and "preupgrade.example.com" in settings_text, settings_text

        # ── "Install" a synthetically-relabeled version N+1 -- delete the
        # old bundle and put a fresh copy in its place (module docstring §8;
        # a real second PyInstaller build would multiply this module's
        # already-heavy setup cost for no additional coverage -- see
        # _bump_bundle_version's own docstring) ───────────────────────────
        shutil.rmtree(app_n)
        app_n1 = _copy_app_from_dmg(install_dir)
        new_version = _bump_bundle_version(app_n1)

        with open(app_n1 / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
        assert info["CFBundleShortVersionString"] == new_version

        _enable_separation(app_n1, user=user)
        daemon = _wait_for_real_daemon()

        # ── State survived the upgrade untouched ──────────────────────────
        settings_text = _sudo_read_text(SEPARATED_SETTINGS_PATH)
        assert settings_text and "preupgrade.example.com" in settings_text, settings_text

        # ── The upgraded bundle still starts and serves, without
        # clobbering the state it just inherited ─────────────────────────
        resp = httpx.get(daemon.bootstrap_url, follow_redirects=True, timeout=10)
        assert resp.status_code == 200, resp.text

        settings_text = _sudo_read_text(SEPARATED_SETTINGS_PATH)
        assert settings_text and "preupgrade.example.com" in settings_text, settings_text
    finally:
        _disable_if_separated(user=user)
        shutil.rmtree(install_dir, ignore_errors=True)
